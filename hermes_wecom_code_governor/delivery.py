from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .policy import Project

MEBIBYTE = 1024 * 1024
WECOM_IMAGE_MAX_BYTES = 10 * MEBIBYTE
WECOM_VIDEO_MAX_BYTES = 10 * MEBIBYTE
WECOM_VOICE_MAX_BYTES = 2 * MEBIBYTE
WECOM_FILE_MAX_BYTES = 50 * MEBIBYTE
WECOM_VOICE_SUPPORTED_MIMES = frozenset({"audio/amr"})
_SAFE_OBJECT_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class CosDeliveryConfig:
    bucket: str
    region: str
    key_prefix: str
    url_expires_seconds: int


@dataclass(frozen=True)
class DeliveryConfig:
    cos: CosDeliveryConfig
    wecom_file_max_bytes: int = WECOM_FILE_MAX_BYTES


@dataclass(frozen=True)
class ArtifactDelivery:
    message_id: str
    channel: Literal["wecom", "cos"]
    path: Path
    filename: str
    size_bytes: int
    download_url: str | None


class CosPublisher(Protocol):
    def publish(self, path: Path, object_key: str) -> str: ...


class TencentCosPublisher:
    def __init__(self, client: object, config: CosDeliveryConfig) -> None:
        self._client = client
        self._config = config

    @classmethod
    def from_environment(cls, config: CosDeliveryConfig) -> TencentCosPublisher:
        secret_id = os.environ.get("COS_SECRET_ID", "").strip()
        secret_key = os.environ.get("COS_SECRET_KEY", "").strip()
        if not secret_id or not secret_key:
            raise RuntimeError("文件交付已启用，但缺少 COS_SECRET_ID 或 COS_SECRET_KEY")
        from qcloud_cos import CosConfig, CosS3Client

        client_config = CosConfig(
            Region=config.region,
            SecretId=secret_id,
            SecretKey=secret_key,
            Scheme="https",
        )
        return cls(CosS3Client(client_config), config)

    def publish(self, path: Path, object_key: str) -> str:
        self._client.upload_file(
            Bucket=self._config.bucket,
            Key=object_key,
            LocalFilePath=str(path),
            EnableMD5=True,
        )
        return str(
            self._client.get_presigned_download_url(
                Bucket=self._config.bucket,
                Key=object_key,
                Expired=self._config.url_expires_seconds,
            )
        )


class FileDeliveryService:
    def __init__(
        self,
        publisher: CosPublisher,
        *,
        key_prefix: str,
        wecom_file_max_bytes: int = WECOM_FILE_MAX_BYTES,
    ) -> None:
        self._publisher = publisher
        self._key_prefix = key_prefix.strip("/")
        self._wecom_file_max_bytes = wecom_file_max_bytes

    def prepare(self, project: Project, requested_path: str, message_id: str) -> ArtifactDelivery:
        path = self._resolve_project_file(project, requested_path)
        return self._prepare_path(path, message_id)

    def prepare_staged(
        self,
        path: Path,
        staging_root: Path,
        message_id: str,
    ) -> ArtifactDelivery:
        root = staging_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise PermissionError("只能交付当前任务暂存目录内的文件")
        if not resolved.is_file():
            raise ValueError("只能交付普通文件")
        return self._prepare_path(resolved, message_id)

    def _prepare_path(self, path: Path, message_id: str) -> ArtifactDelivery:
        size_bytes = path.stat().st_size
        if size_bytes <= self._wecom_file_max_bytes:
            return ArtifactDelivery(
                message_id=message_id,
                channel="wecom",
                path=path,
                filename=path.name,
                size_bytes=size_bytes,
                download_url=None,
            )
        object_key = self._object_key(message_id, path.name)
        download_url = self._publisher.publish(path, object_key)
        return ArtifactDelivery(
            message_id=message_id,
            channel="cos",
            path=path,
            filename=path.name,
            size_bytes=size_bytes,
            download_url=download_url,
        )

    @staticmethod
    def _resolve_project_file(project: Project, requested_path: str) -> Path:
        value = requested_path.strip()
        if not value:
            raise ValueError("文件路径不能为空")
        project_root = project.path.resolve()
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = project_root / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"文件不存在：{value}") from exc
        if not resolved.is_relative_to(project_root):
            raise PermissionError("只能交付当前已选项目目录内的文件")
        if not resolved.is_file():
            raise ValueError("只能交付普通文件")
        return resolved

    def _object_key(self, message_id: str, filename: str) -> str:
        safe_message_id = _SAFE_OBJECT_SEGMENT.sub("-", message_id).strip("-.")
        if not safe_message_id:
            raise ValueError("message_id 缺少可用字符")
        segments = [segment for segment in (self._key_prefix, safe_message_id, filename) if segment]
        return "/".join(segments)


def apply_governed_file_size_limits(
    file_size: int,
    detected_type: str,
    content_type: str | None = None,
) -> dict[str, object]:
    file_size_mb = file_size / MEBIBYTE
    normalized_type = str(detected_type or "file").lower()
    normalized_content_type = str(content_type or "").strip().lower()

    if file_size > WECOM_FILE_MAX_BYTES:
        return {
            "final_type": normalized_type,
            "rejected": True,
            "reject_reason": (
                f"文件大小 {file_size_mb:.2f}MB 超过了企业微信 AI Bot 原生文件限制 50MB。"
            ),
            "downgraded": False,
            "downgrade_note": None,
        }
    if normalized_type == "image" and file_size > WECOM_IMAGE_MAX_BYTES:
        return {
            "final_type": "file",
            "rejected": False,
            "reject_reason": None,
            "downgraded": True,
            "downgrade_note": f"图片大小 {file_size_mb:.2f}MB 超过 10MB，已按文件发送",
        }
    if normalized_type == "video" and file_size > WECOM_VIDEO_MAX_BYTES:
        return {
            "final_type": "file",
            "rejected": False,
            "reject_reason": None,
            "downgraded": True,
            "downgrade_note": f"视频大小 {file_size_mb:.2f}MB 超过 10MB，已按文件发送",
        }
    if normalized_type == "voice":
        unsupported = (
            normalized_content_type and normalized_content_type not in WECOM_VOICE_SUPPORTED_MIMES
        )
        if unsupported or file_size > WECOM_VOICE_MAX_BYTES:
            return {
                "final_type": "file",
                "rejected": False,
                "reject_reason": None,
                "downgraded": True,
                "downgrade_note": "语音格式或大小不满足企微语音限制，已按文件发送",
            }
    return {
        "final_type": normalized_type,
        "rejected": False,
        "reject_reason": None,
        "downgraded": False,
        "downgrade_note": None,
    }
