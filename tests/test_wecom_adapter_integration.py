from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("gateway.config")

from gateway.config import PlatformConfig

from hermes_wecom_code_governor.policy import Identity
from hermes_wecom_code_governor.wecom_adapter import create_governed_adapter_class


class FakeRuntime:
    def __init__(self) -> None:
        self.identity = Identity("user-1", "chat-1", "group")
        self.authorized_users = {"user-1", "trusted-user"}
        self.selections: list[tuple[str, Identity, str]] = []
        self.notifier = None

    def set_notifier(self, notifier: object) -> None:
        self.notifier = notifier

    def is_authorized_identity(self, identity: Identity) -> bool:
        return identity.user_id in self.authorized_users

    def project_choices_for_session(self, session_key: str) -> dict[str, str]:
        assert session_key == "session-1"
        return {"aijd": "AIJD测试项目", "vpp": "VPP数字孪生项目"}

    def session_record_for_adapter(self, session_key: str) -> object:
        assert session_key == "session-1"
        return SimpleNamespace(identity=self.identity)

    def select_project_for_session(
        self, session_key: str, identity: Identity, project_id: str
    ) -> dict[str, str]:
        self.selections.append((session_key, identity, project_id))
        return {"project_id": project_id}


def card_event(*, user_id: str = "user-1") -> dict:
    return {
        "cmd": "aibot_event_callback",
        "headers": {"req_id": "event-req"},
        "body": {
            "from": {"userid": user_id},
            "chatid": "chat-1",
            "chattype": "group",
            "event": {"template_card_event": {"task_id": "clarify-1", "event_key": "vpp"}},
        },
    }


def test_project_clarify_uses_native_card_and_valid_click_resumes_waiter() -> None:
    runtime = FakeRuntime()
    adapter_class = create_governed_adapter_class(runtime)
    adapter = adapter_class(PlatformConfig(enabled=True, extra={"bot_id": "id", "secret": "s"}))
    adapter._last_chat_req_ids["chat-1"] = "message-req"
    adapter._send_reply_request = AsyncMock(return_value={"headers": {"req_id": "reply"}})

    result = asyncio.run(
        adapter.send_clarify(
            "chat-1",
            "选哪个项目？",
            ["AIJD测试项目 (Recommended)", "VPP数字孪生项目"],
            "clarify-1",
            "session-1",
        )
    )
    assert result.success
    first_body = adapter._send_reply_request.await_args_list[0].args[1]
    assert first_body["template_card"] == {
        "card_type": "button_interaction",
        "main_title": {"title": "请选择要处理的项目"},
        "horizontal_content_list": [
            {"keyname": "选项1", "value": "AIJD测试项目"},
            {"keyname": "选项2", "value": "VPP数字孪生项目"},
        ],
        "button_list": [
            {"text": "选项1", "key": "aijd", "style": 1},
            {"text": "选项2", "key": "vpp", "style": 1},
        ],
        "task_id": "clarify-1",
    }

    with patch("tools.clarify_gateway.resolve_gateway_clarify", return_value=True) as resolve:
        asyncio.run(adapter._dispatch_payload(card_event()))

    assert runtime.selections == [("session-1", runtime.identity, "vpp")]
    resolve.assert_called_once_with("clarify-1", "VPP数字孪生项目")
    update_body = adapter._send_reply_request.await_args_list[1].args[1]
    assert update_body["template_card"]["main_title"]["title"] == (
        "已选择项目：VPP数字孪生项目，正在处理。"
    )
    assert update_body["userids"] == ["user-1"]
    confirmation_call = adapter._send_reply_request.await_args_list[2]
    assert confirmation_call.args[0] == "message-req"
    assert confirmation_call.args[1] == {
        "msgtype": "markdown",
        "markdown": {"content": "已选择项目：VPP数字孪生项目，正在处理。"},
    }


def test_other_group_member_cannot_resolve_someone_elses_card() -> None:
    runtime = FakeRuntime()
    adapter_class = create_governed_adapter_class(runtime)
    adapter = adapter_class(PlatformConfig(enabled=True, extra={"bot_id": "id", "secret": "s"}))
    adapter._last_chat_req_ids["chat-1"] = "message-req"
    adapter._send_reply_request = AsyncMock(return_value={"headers": {"req_id": "reply"}})
    asyncio.run(
        adapter.send_clarify(
            "chat-1",
            "选哪个项目？",
            ["AIJD测试项目", "VPP数字孪生项目"],
            "clarify-1",
            "session-1",
        )
    )

    with patch("tools.clarify_gateway.resolve_gateway_clarify") as resolve:
        asyncio.run(adapter._dispatch_payload(card_event(user_id="user-2")))

    assert runtime.selections == []
    resolve.assert_not_called()
    title = adapter._send_reply_request.await_args_list[1].args[1]["template_card"]["main_title"][
        "title"
    ]
    assert title == "这张项目选择卡片不属于你。"


def test_wecom_intake_uses_governor_permission_users_instead_of_a_copied_user_list() -> None:
    runtime = FakeRuntime()
    adapter_class = create_governed_adapter_class(runtime)
    adapter = adapter_class(
        PlatformConfig(
            enabled=True,
            extra={
                "bot_id": "id",
                "secret": "s",
                "dm_policy": "allowlist",
                "allow_from": ["user-1"],
                "group_policy": "allowlist",
                "group_allow_from": ["chat-1"],
            },
        )
    )

    assert adapter._is_dm_intake_allowed("trusted-user")
    assert adapter._is_dm_allowed("trusted-user")
    assert adapter._is_group_allowed("any-chat", "trusted-user")
    assert not adapter._is_dm_intake_allowed("stranger")
    assert not adapter._is_group_allowed("any-chat", "stranger")


def test_runtime_notice_is_delivered_as_a_plain_chat_message() -> None:
    runtime = FakeRuntime()
    adapter_class = create_governed_adapter_class(runtime)
    adapter = adapter_class(PlatformConfig(enabled=True, extra={"bot_id": "id", "secret": "s"}))
    adapter._last_chat_req_ids["chat-1"] = "message-req"
    adapter._send_reply_request = AsyncMock(return_value={"headers": {"req_id": "reply"}})

    async def exercise() -> None:
        await adapter._dispatch_payload({"cmd": "unrelated"})
        assert runtime.notifier is not None
        await asyncio.to_thread(
            runtime.notifier,
            runtime.identity,
            "正在查看 VPP数字孪生项目，请稍候。",
        )
        await asyncio.sleep(0)

    asyncio.run(exercise())

    body = adapter._send_reply_request.await_args.args[1]
    assert body == {
        "msgtype": "markdown",
        "markdown": {"content": "正在查看 VPP数字孪生项目，请稍候。"},
    }
