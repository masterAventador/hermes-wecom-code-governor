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


def test_runtime_notice_quotes_the_triggering_message() -> None:
    runtime = FakeRuntime()
    adapter_class = create_governed_adapter_class(runtime)
    adapter = adapter_class(PlatformConfig(enabled=True, extra={"bot_id": "id", "secret": "s"}))
    adapter.send = AsyncMock()
    base_class = type(adapter).__mro__[1]

    async def exercise() -> None:
        with patch.object(base_class, "_dispatch_payload", new=AsyncMock()):
            await adapter._dispatch_payload(
                message_payload(user_id="trusted-user", msg_id="trigger-1")
            )
        assert runtime.notifier is not None
        await asyncio.to_thread(runtime.notifier, runtime.identity, "正在修改 X，请稍候。")
        await asyncio.sleep(0)

    asyncio.run(exercise())

    call = adapter.send.await_args
    assert call.kwargs.get("reply_to") == "trigger-1"


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


def message_payload(
    *,
    user_id: str,
    chat_id: str | None = "chat-1",
    chat_type: str = "group",
    msg_id: str = "msg-1",
) -> dict:
    body: dict = {
        "msgid": msg_id,
        "from": {"userid": user_id},
        "chattype": chat_type,
        "text": {"content": "@机器人 帮我改代码"},
    }
    if chat_id is not None:
        body["chatid"] = chat_id
    return {"cmd": "aibot_callback", "headers": {"req_id": f"req-{msg_id}"}, "body": body}


def test_unauthorized_group_mention_replies_ids_every_time_and_never_reaches_base() -> None:
    runtime = FakeRuntime()
    adapter_class = create_governed_adapter_class(runtime)
    adapter = adapter_class(PlatformConfig(enabled=True, extra={"bot_id": "id", "secret": "s"}))
    adapter.send = AsyncMock()
    base_class = type(adapter).__mro__[1]

    async def exercise() -> None:
        with patch.object(base_class, "_on_message", new=AsyncMock()) as base_on_message:
            await adapter._on_message(message_payload(user_id="stranger-9", msg_id="msg-1"))
            await adapter._on_message(message_payload(user_id="stranger-9", msg_id="msg-2"))
            # 同一条消息的协议层重复投递不重复提示
            await adapter._on_message(message_payload(user_id="stranger-9", msg_id="msg-2"))
            assert base_on_message.await_count == 0

    asyncio.run(exercise())

    assert adapter.send.await_count == 2
    first = adapter.send.await_args_list[0]
    assert first.args[0] == "chat-1"
    assert "stranger-9" in first.args[1]
    assert "chat-1" in first.args[1]
    assert first.kwargs.get("reply_to") == "msg-1"


def test_unauthorized_mention_is_logged_for_observability(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gateway.platforms.base import SendResult

    runtime = FakeRuntime()
    adapter_class = create_governed_adapter_class(runtime)
    adapter = adapter_class(PlatformConfig(enabled=True, extra={"bot_id": "id", "secret": "s"}))
    adapter.send = AsyncMock(return_value=SendResult(success=True))

    with caplog.at_level("INFO", logger="hermes_wecom_code_governor.wecom_adapter"):
        asyncio.run(adapter._on_message(message_payload(user_id="stranger-9", msg_id="m-1")))

    logged = [record.getMessage() for record in caplog.records]
    assert any("未授权" in message and "stranger-9" in message for message in logged)


def test_unauthorized_notice_send_failure_is_logged_not_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gateway.platforms.base import SendResult

    runtime = FakeRuntime()
    adapter_class = create_governed_adapter_class(runtime)
    adapter = adapter_class(PlatformConfig(enabled=True, extra={"bot_id": "id", "secret": "s"}))
    adapter.send = AsyncMock(return_value=SendResult(success=False, error="no reply channel"))

    with caplog.at_level("WARNING", logger="hermes_wecom_code_governor.wecom_adapter"):
        asyncio.run(adapter._on_message(message_payload(user_id="stranger-9", msg_id="m-2")))

    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert any("no reply channel" in message for message in warnings)


def test_unauthorized_dm_notice_uses_sender_as_chat_id() -> None:
    runtime = FakeRuntime()
    adapter_class = create_governed_adapter_class(runtime)
    adapter = adapter_class(PlatformConfig(enabled=True, extra={"bot_id": "id", "secret": "s"}))
    adapter.send = AsyncMock()

    asyncio.run(
        adapter._on_message(message_payload(user_id="stranger-9", chat_id=None, chat_type="single"))
    )

    assert adapter.send.await_count == 1
    call = adapter.send.await_args
    assert call.args[0] == "stranger-9"
    assert "stranger-9" in call.args[1]


def test_authorized_message_passes_through_to_base_without_notice() -> None:
    runtime = FakeRuntime()
    adapter_class = create_governed_adapter_class(runtime)
    adapter = adapter_class(PlatformConfig(enabled=True, extra={"bot_id": "id", "secret": "s"}))
    adapter.send = AsyncMock()
    base_class = type(adapter).__mro__[1]

    async def exercise() -> None:
        with patch.object(base_class, "_on_message", new=AsyncMock()) as base_on_message:
            await adapter._on_message(message_payload(user_id="trusted-user"))
            assert base_on_message.await_count == 1

    asyncio.run(exercise())

    assert adapter.send.await_count == 0
