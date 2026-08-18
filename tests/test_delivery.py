from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_wecom_code_governor.delivery import (
    WECOM_FILE_MAX_BYTES,
    ArtifactDelivery,
    CosDeliveryConfig,
    FileDeliveryService,
    LazyTencentCosPublisher,
    TencentCosPublisher,
    apply_governed_file_size_limits,
)
from hermes_wecom_code_governor.policy import Project


@dataclass
class FakeCosPublisher:
    url: str = "https://example.cos.invalid/download"

    def __post_init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def publish(self, path: Path, object_key: str) -> str:
        self.calls.append((path, object_key))
        return self.url


def test_lazy_tencent_cos_publisher_initializes_sdk_on_first_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = CosDeliveryConfig("bucket", "region", "prefix", 3600)
    publisher = FakeCosPublisher()
    initializations: list[CosDeliveryConfig] = []

    def initialize(received: CosDeliveryConfig) -> FakeCosPublisher:
        initializations.append(received)
        return publisher

    monkeypatch.setattr(
        TencentCosPublisher,
        "from_environment",
        staticmethod(initialize),
    )
    lazy = LazyTencentCosPublisher(config)
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"artifact")

    assert initializations == []
    assert lazy.publish(artifact, "one") == publisher.url
    assert lazy.publish(artifact, "two") == publisher.url
    assert initializations == [config]
    assert publisher.calls == [(artifact, "one"), (artifact, "two")]


def project(path: Path) -> Project:
    return Project("desktop", "桌面客户端", path)


def test_small_project_file_is_prepared_for_native_wecom_delivery(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    artifact = release / "client.zip"
    artifact.write_bytes(b"small artifact")
    publisher = FakeCosPublisher()
    service = FileDeliveryService(publisher, key_prefix="electron-builds")

    result = service.prepare(project(tmp_path), "release/client.zip", "message-1")

    assert result == ArtifactDelivery(
        message_id="message-1",
        channel="wecom",
        path=artifact,
        filename="client.zip",
        size_bytes=len(b"small artifact"),
        download_url=None,
    )
    assert publisher.calls == []


def test_exact_50_mib_stays_in_wecom_and_larger_file_uses_cos(tmp_path: Path) -> None:
    exact = tmp_path / "exact.bin"
    exact.write_bytes(b"")
    exact.touch()
    exact.chmod(0o600)
    with exact.open("r+b") as handle:
        handle.truncate(WECOM_FILE_MAX_BYTES)
    larger = tmp_path / "larger.bin"
    larger.write_bytes(b"")
    with larger.open("r+b") as handle:
        handle.truncate(WECOM_FILE_MAX_BYTES + 1)
    publisher = FakeCosPublisher()
    service = FileDeliveryService(publisher, key_prefix="/electron-builds/")

    native = service.prepare(project(tmp_path), "exact.bin", "message-native")
    cloud = service.prepare(project(tmp_path), "larger.bin", "message-cloud")

    assert native.channel == "wecom"
    assert cloud.channel == "cos"
    assert cloud.download_url == publisher.url
    assert publisher.calls == [(larger, "electron-builds/message-cloud/larger.bin")]


def test_delivery_rejects_paths_outside_the_selected_project(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    service = FileDeliveryService(FakeCosPublisher(), key_prefix="files")

    with pytest.raises(PermissionError, match="项目目录"):
        service.prepare(project(repository), "../secret.txt", "message-1")


def test_staged_job_artifact_uses_the_same_wecom_or_cos_delivery_policy(
    tmp_path: Path,
) -> None:
    staging_root = tmp_path / "artifacts" / "job-1"
    staging_root.mkdir(parents=True)
    artifact = staging_root / "client.exe"
    artifact.write_bytes(b"large-enough-for-test")
    publisher = FakeCosPublisher()
    service = FileDeliveryService(
        publisher,
        key_prefix="electron-builds",
        wecom_file_max_bytes=10,
    )

    result = service.prepare_staged(artifact, staging_root, "message-job")

    assert result.channel == "cos"
    assert result.filename == "client.exe"
    assert result.download_url == publisher.url
    assert publisher.calls == [(artifact, "electron-builds/message-job/client.exe")]


def test_staged_job_artifact_cannot_escape_its_staging_root(tmp_path: Path) -> None:
    staging_root = tmp_path / "artifacts" / "job-1"
    staging_root.mkdir(parents=True)
    outside = tmp_path / "outside.exe"
    outside.write_bytes(b"outside")
    service = FileDeliveryService(FakeCosPublisher(), key_prefix="files")

    with pytest.raises(PermissionError, match="暂存目录"):
        service.prepare_staged(outside, staging_root, "message-job")


def test_wecom_policy_uses_official_50_mib_file_limit_and_media_downgrade() -> None:
    accepted = apply_governed_file_size_limits(50 * 1024 * 1024, "file")
    rejected = apply_governed_file_size_limits(50 * 1024 * 1024 + 1, "file")
    image_as_file = apply_governed_file_size_limits(
        11 * 1024 * 1024,
        "image",
        "image/png",
    )

    assert accepted["rejected"] is False
    assert rejected["rejected"] is True
    assert "50MB" in rejected["reject_reason"]
    assert image_as_file["rejected"] is False
    assert image_as_file["final_type"] == "file"
    assert image_as_file["downgraded"] is True


def test_tencent_cos_publisher_uploads_and_returns_a_signed_download_url(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "app.exe"
    artifact.write_bytes(b"artifact")

    class FakeClient:
        def __init__(self) -> None:
            self.upload: dict | None = None
            self.signed: dict | None = None

        def upload_file(self, **kwargs: object) -> None:
            self.upload = kwargs

        def get_presigned_download_url(self, **kwargs: object) -> str:
            self.signed = kwargs
            return "https://cos.example/signed"

    client = FakeClient()
    publisher = TencentCosPublisher(
        client,
        CosDeliveryConfig(
            bucket="bucket-123",
            region="ap-beijing",
            key_prefix="electron-builds",
            url_expires_seconds=604_800,
        ),
    )

    url = publisher.publish(artifact, "electron-builds/task/app.exe")

    assert url == "https://cos.example/signed"
    assert client.upload == {
        "Bucket": "bucket-123",
        "Key": "electron-builds/task/app.exe",
        "LocalFilePath": str(artifact),
        "EnableMD5": True,
    }
    assert client.signed == {
        "Bucket": "bucket-123",
        "Key": "electron-builds/task/app.exe",
        "Expired": 604_800,
    }
