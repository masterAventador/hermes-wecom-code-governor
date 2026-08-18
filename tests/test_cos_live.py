from __future__ import annotations

import os
import uuid
import warnings
from pathlib import Path

import pytest
import requests
from qcloud_cos.cos_exception import CosServiceError

from hermes_wecom_code_governor.config import load_governor_config
from hermes_wecom_code_governor.delivery import FileDeliveryService, TencentCosPublisher

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_COS_LIVE") != "1",
    reason="set RUN_COS_LIVE=1 to use the configured Tencent COS bucket",
)


def load_secret_environment(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("\"'")


def test_real_cos_upload_signed_download_and_cleanup(tmp_path: Path) -> None:
    secret_file = Path(os.environ["GOVERNOR_SECRET_ENV_FILE"])
    load_secret_environment(secret_file)
    config = load_governor_config(Path(__file__).parents[1] / "config" / "governor.local.yaml")
    assert config.delivery is not None
    publisher = TencentCosPublisher.from_environment(config.delivery.cos)
    payload = f"hermes-governor-cos-smoke-{uuid.uuid4().hex}".encode()
    artifact = tmp_path / "cos-smoke.txt"
    artifact.write_bytes(payload)
    object_key = os.environ.get(
        "GOVERNOR_COS_SMOKE_OBJECT_KEY",
        f"{config.delivery.cos.key_prefix}/smoke-tests/hermes-governor-current.txt",
    )

    try:
        url = publisher.publish(artifact, object_key)
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        assert response.content == payload
    finally:
        try:
            publisher._client.delete_object(
                Bucket=config.delivery.cos.bucket,
                Key=object_key,
            )
        except CosServiceError as exc:
            if exc.get_error_code() != "AccessDenied":
                raise
            warnings.warn(
                "COS smoke object retained because this delivery-only account lacks DeleteObject",
                RuntimeWarning,
                stacklevel=2,
            )


@pytest.mark.skipif(
    os.environ.get("RUN_COS_LARGE_LIVE") != "1",
    reason="set RUN_COS_LARGE_LIVE=1 to upload the real VPP installer",
)
def test_real_large_installer_uses_cos_and_signed_url_can_read_content() -> None:
    secret_file = Path(os.environ["GOVERNOR_SECRET_ENV_FILE"])
    load_secret_environment(secret_file)
    config = load_governor_config(Path(__file__).parents[1] / "config" / "governor.local.yaml")
    assert config.delivery is not None
    project = config.policy.project("vpp-digital-twin")
    artifact = project.path / "release" / "vpp-digital-twin-1.2.5-setup.exe"
    assert artifact.stat().st_size > 100 * 1024 * 1024
    publisher = TencentCosPublisher.from_environment(config.delivery.cos)
    service = FileDeliveryService(
        publisher,
        key_prefix=config.delivery.cos.key_prefix,
        wecom_file_max_bytes=config.delivery.wecom_file_max_bytes,
    )

    delivery = service.prepare_staged(
        artifact,
        project.path / "release",
        "0818-vpp-package-acceptance",
    )

    assert delivery.channel == "cos"
    assert delivery.download_url is not None
    with requests.get(
        delivery.download_url,
        headers={"Range": "bytes=0-31"},
        timeout=30,
        stream=True,
    ) as response:
        response.raise_for_status()
        assert int(response.headers["Content-Length"]) in {32, artifact.stat().st_size}
        with artifact.open("rb") as handle:
            assert response.raw.read(32) == handle.read(32)
