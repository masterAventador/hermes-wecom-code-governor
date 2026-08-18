from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace

from hermes_wecom_code_governor.delivery import WECOM_FILE_MAX_BYTES, ArtifactDelivery
from hermes_wecom_code_governor.wecom_adapter import create_governed_adapter_class


@dataclass
class SendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None
    raw_response: object = None


class FakeWeComAdapter:
    def __init__(self, config: object) -> None:
        self.config = config
        self.text_calls: list[dict] = []
        self.document_calls: list[dict] = []

    async def send(self, **kwargs: object) -> SendResult:
        self.text_calls.append(dict(kwargs))
        return SendResult(True, message_id="text-1")

    async def send_document(self, **kwargs: object) -> SendResult:
        self.document_calls.append(dict(kwargs))
        return SendResult(True, message_id="file-1")


class FakeRuntime:
    def __init__(self, deliveries: list[ArtifactDelivery]) -> None:
        self.deliveries = deliveries

    def take_pending_delivery(self, message_id: str) -> ArtifactDelivery | None:
        assert message_id == "message-1"
        return self.deliveries.pop(0) if self.deliveries else None


def install_fake_hermes_modules() -> None:
    base = ModuleType("gateway.platforms.base")
    base.SendResult = SendResult
    adapter = ModuleType("plugins.platforms.wecom.adapter")
    adapter.WeComAdapter = FakeWeComAdapter
    modules = {
        "gateway": ModuleType("gateway"),
        "gateway.platforms": ModuleType("gateway.platforms"),
        "gateway.platforms.base": base,
        "plugins": ModuleType("plugins"),
        "plugins.platforms": ModuleType("plugins.platforms"),
        "plugins.platforms.wecom": ModuleType("plugins.platforms.wecom"),
        "plugins.platforms.wecom.adapter": adapter,
    }
    sys.modules.update(modules)


def make_adapter(delivery: ArtifactDelivery | list[ArtifactDelivery]):
    install_fake_hermes_modules()
    deliveries = delivery if isinstance(delivery, list) else [delivery]
    runtime = FakeRuntime(deliveries)
    adapter_class = create_governed_adapter_class(runtime)
    return adapter_class(SimpleNamespace()), runtime


def test_final_reply_sends_small_file_through_native_wecom_document() -> None:
    delivery = ArtifactDelivery(
        message_id="message-1",
        channel="wecom",
        path=Path("/project/release/app.zip"),
        filename="app.zip",
        size_bytes=1024,
        download_url=None,
    )
    adapter, _ = make_adapter(delivery)

    result = asyncio.run(
        adapter.send(
            chat_id="chat-1",
            content="文件已经准备好。",
            reply_to="message-1",
            metadata={"notify": True},
        )
    )

    assert result.success
    assert adapter.document_calls == [
        {
            "chat_id": "chat-1",
            "file_path": "/project/release/app.zip",
            "file_name": "app.zip",
            "reply_to": "message-1",
        }
    ]


def test_final_reply_appends_cos_download_url_without_native_file_send() -> None:
    delivery = ArtifactDelivery(
        message_id="message-1",
        channel="cos",
        path=Path("/project/release/app.exe"),
        filename="app.exe",
        size_bytes=WECOM_FILE_MAX_BYTES + 1,
        download_url="https://cos.example/signed",
    )
    adapter, _ = make_adapter(delivery)

    asyncio.run(
        adapter.send(
            chat_id="chat-1",
            content="文件已经准备好。",
            reply_to="message-1",
            metadata={"notify": True},
        )
    )

    assert adapter.text_calls[0]["content"] == (
        "文件已经准备好。\n\n[下载 Windows 安装包](https://cos.example/signed)"
    )
    assert adapter.document_calls == []


def test_final_reply_does_not_duplicate_cos_link_already_written_by_model() -> None:
    delivery = ArtifactDelivery(
        message_id="message-1",
        channel="cos",
        path=Path("/project/release/app.exe"),
        filename="app.exe",
        size_bytes=WECOM_FILE_MAX_BYTES + 1,
        download_url="https://cos.example/signed",
    )
    adapter, _ = make_adapter(delivery)
    content = (
        "Windows 安装包已重新打包完成并交付：\n\n[下载 Windows 安装包](https://cos.example/signed)"
    )

    asyncio.run(
        adapter.send(
            chat_id="chat-1",
            content=content,
            reply_to="message-1",
            metadata={"notify": True},
        )
    )

    assert adapter.text_calls[0]["content"] == content


def test_final_reply_delivers_every_artifact_queued_for_the_same_job() -> None:
    native = ArtifactDelivery(
        message_id="message-1",
        channel="wecom",
        path=Path("/project/release/report.txt"),
        filename="report.txt",
        size_bytes=1024,
        download_url=None,
    )
    first_cloud = ArtifactDelivery(
        message_id="message-1",
        channel="cos",
        path=Path("/project/release/app.exe"),
        filename="app.exe",
        size_bytes=WECOM_FILE_MAX_BYTES + 1,
        download_url="https://cos.example/app",
    )
    second_cloud = ArtifactDelivery(
        message_id="message-1",
        channel="cos",
        path=Path("/project/release/symbols.zip"),
        filename="symbols.zip",
        size_bytes=WECOM_FILE_MAX_BYTES + 1,
        download_url="https://cos.example/symbols",
    )
    adapter, runtime = make_adapter([native, first_cloud, second_cloud])

    asyncio.run(
        adapter.send(
            chat_id="chat-1",
            content="产物已经准备好。",
            reply_to="message-1",
            metadata={"notify": True},
        )
    )

    content = adapter.text_calls[0]["content"]
    assert "https://cos.example/app" in content
    assert "https://cos.example/symbols" in content
    assert [call["file_name"] for call in adapter.document_calls] == ["report.txt"]
    assert runtime.deliveries == []


def test_governed_adapter_overrides_hermes_20_mib_limit_with_official_50_mib() -> None:
    delivery = ArtifactDelivery(
        message_id="message-1",
        channel="wecom",
        path=Path("/project/release/app.zip"),
        filename="app.zip",
        size_bytes=1024,
        download_url=None,
    )
    adapter, _ = make_adapter(delivery)

    accepted = adapter._apply_file_size_limits(WECOM_FILE_MAX_BYTES, "file")
    rejected = adapter._apply_file_size_limits(WECOM_FILE_MAX_BYTES + 1, "file")

    assert accepted["rejected"] is False
    assert rejected["rejected"] is True
