from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from .delivery import apply_governed_file_size_limits
from .policy import Identity
from .runtime import GovernorRuntime
from .wecom_cards import CardEvent, match_project_choices, parse_card_event

APP_CMD_EVENT_CALLBACK = "aibot_event_callback"
APP_CMD_RESPONSE = "aibot_respond_msg"
APP_CMD_UPDATE_RESPONSE = "aibot_respond_update_msg"

logger = logging.getLogger(__name__)


def _download_link_label(filename: str) -> str:
    if filename.casefold().endswith(".exe"):
        return "下载 Windows 安装包"
    return f"下载 {filename}"


@dataclass(frozen=True)
class _PendingProjectCard:
    session_key: str
    identity: Identity
    projects: dict[str, str]


def create_governed_adapter_class(runtime: GovernorRuntime):
    # Hermes is an installation dependency of the deployed plugin, but not of
    # the policy unit-test package. Import it only when Hermes materializes the
    # configured platform adapter.
    from gateway.platforms.base import SendResult
    from plugins.platforms.wecom.adapter import WeComAdapter

    class GovernedWeComAdapter(WeComAdapter):
        def __init__(self, config: Any):
            super().__init__(config)
            self._governor_cards: dict[str, _PendingProjectCard] = {}

        async def _on_message(self, payload: dict[str, Any]) -> None:
            body = payload.get("body")
            if isinstance(body, dict):
                sender = body.get("from") if isinstance(body.get("from"), dict) else {}
                sender_id = str(sender.get("userid") or "").strip()
                chat_id = str(body.get("chatid") or sender_id).strip()
                chat_type = "group" if str(body.get("chattype") or "").lower() == "group" else "dm"
                if (
                    sender_id
                    and chat_id
                    and not runtime.is_authorized_identity(Identity(sender_id, chat_id, chat_type))
                ):
                    await self._notify_unauthorized(payload, sender_id, chat_id)
                    return
            await super()._on_message(payload)

        async def _notify_unauthorized(
            self,
            payload: dict[str, Any],
            sender_id: str,
            chat_id: str,
        ) -> None:
            # 每次 @ 都回复（管理员靠这条拿 userid 开权限，宁可多发不可漏发）；
            # 仅对同一条消息的协议层重复投递去重。
            body = payload.get("body")
            msg_id = str(body.get("msgid") or "").strip() if isinstance(body, dict) else ""
            if msg_id and self._dedup.is_duplicate(msg_id):
                return
            if msg_id:
                # 群里无法主动发消息，必须借这条消息自带的响应通道回话。
                self._remember_reply_req_id(msg_id, self._payload_req_id(payload))
            notice = (
                "你还没有这个机器人的使用权限。请把下面两个 ID 发给管理员开通：\n"
                f"userid：{sender_id}\n"
                f"chatid：{chat_id}"
            )
            try:
                await self.send(chat_id, notice, reply_to=msg_id or None)
            except Exception:
                logger.warning("未授权提示发送失败 user=%s chat=%s", sender_id, chat_id)

        def _is_dm_allowed(self, sender_id: str) -> bool:
            principal = str(sender_id or "").strip()
            return bool(principal) and runtime.is_authorized_identity(
                Identity(principal, principal, "dm")
            )

        def _is_dm_intake_allowed(self, sender_id: str) -> bool:
            return self._is_dm_allowed(sender_id)

        def _is_group_allowed(self, chat_id: str, sender_id: str) -> bool:
            chat = str(chat_id or "").strip()
            principal = str(sender_id or "").strip()
            return bool(chat and principal) and runtime.is_authorized_identity(
                Identity(principal, chat, "group")
            )

        @staticmethod
        def _apply_file_size_limits(
            file_size: int,
            detected_type: str,
            content_type: str | None = None,
        ) -> dict[str, object]:
            return apply_governed_file_size_limits(
                file_size,
                detected_type,
                content_type,
            )

        async def send(
            self,
            chat_id: str,
            content: str,
            reply_to: str | None = None,
            metadata: dict | None = None,
        ) -> SendResult:
            deliveries = []
            if metadata and metadata.get("notify") and reply_to:
                while delivery := runtime.take_pending_delivery(str(reply_to)):
                    deliveries.append(delivery)
            for delivery in deliveries:
                if delivery.channel == "cos":
                    download_url = delivery.download_url
                    if download_url and download_url not in content:
                        label = _download_link_label(delivery.filename)
                        content += f"\n\n[{label}]({download_url})"
            text_result = await super().send(
                chat_id=chat_id,
                content=content,
                reply_to=reply_to,
                metadata=metadata,
            )
            if text_result.success:
                for delivery in deliveries:
                    if delivery.channel != "wecom":
                        continue
                    file_result = await super().send_document(
                        chat_id=chat_id,
                        file_path=str(delivery.path),
                        file_name=delivery.filename,
                        reply_to=reply_to,
                    )
                    if not file_result.success:
                        await super().send(
                            chat_id=chat_id,
                            content=f"文件 {delivery.filename} 发送失败：{file_result.error}",
                            reply_to=reply_to,
                        )
            return text_result

        async def send_clarify(
            self,
            chat_id: str,
            question: str,
            choices: list | None,
            clarify_id: str,
            session_key: str,
            metadata: dict | None = None,
        ) -> SendResult:
            del metadata
            projects = runtime.project_choices_for_session(session_key)
            buttons = match_project_choices(choices, projects)
            if buttons is None:
                return await super().send_clarify(
                    chat_id=chat_id,
                    question=question,
                    choices=choices,
                    clarify_id=clarify_id,
                    session_key=session_key,
                )

            record = runtime.session_record_for_adapter(session_key)
            if record is None:
                return await super().send_clarify(
                    chat_id=chat_id,
                    question=question,
                    choices=choices,
                    clarify_id=clarify_id,
                    session_key=session_key,
                )
            reply_req_id = self._last_chat_req_ids.get(chat_id)
            if not reply_req_id:
                return await super().send_clarify(
                    chat_id=chat_id,
                    question=question,
                    choices=choices,
                    clarify_id=clarify_id,
                    session_key=session_key,
                )

            card = {
                "card_type": "button_interaction",
                "main_title": {"title": "请选择要处理的项目"},
                "horizontal_content_list": [
                    {"keyname": f"选项{index}", "value": display_name}
                    for index, (_, display_name) in enumerate(buttons, start=1)
                ],
                "button_list": [
                    {"text": f"选项{index}", "key": project_id, "style": 1}
                    for index, (project_id, _) in enumerate(buttons, start=1)
                ],
                "task_id": clarify_id,
            }
            try:
                response = await self._send_reply_request(
                    reply_req_id,
                    {"msgtype": "template_card", "template_card": card},
                    cmd=APP_CMD_RESPONSE,
                )
            except TimeoutError:
                return SendResult(success=False, error="Timeout sending project card to WeCom")
            except Exception as exc:
                return SendResult(success=False, error=str(exc))
            error = self._response_error(response)
            if error:
                return SendResult(success=False, error=error)

            self._governor_cards[clarify_id] = _PendingProjectCard(
                session_key=session_key,
                identity=record.identity,
                projects=dict(buttons),
            )
            return SendResult(
                success=True,
                message_id=self._payload_req_id(response) or uuid.uuid4().hex[:12],
                raw_response=response,
            )

        async def _dispatch_payload(self, payload: dict[str, Any]) -> None:
            loop = asyncio.get_running_loop()

            def notify(identity: Identity, content: str) -> None:
                if not identity.chat_id or loop.is_closed():
                    return
                future = asyncio.run_coroutine_threadsafe(
                    self.send(chat_id=identity.chat_id, content=content),
                    loop,
                )

                def consume_result(completed: object) -> None:
                    try:
                        result = completed.result()
                    except Exception:
                        logger.exception("Failed to deliver governor processing notice")
                        return
                    if not result.success:
                        logger.warning(
                            "Failed to deliver governor processing notice: %s",
                            result.error,
                        )

                future.add_done_callback(consume_result)

            runtime.set_notifier(notify)
            event = parse_card_event(payload)
            if event is not None:
                await self._handle_governor_card(event)
                return
            await super()._dispatch_payload(payload)

        async def _handle_governor_card(self, event: CardEvent) -> None:
            pending = self._governor_cards.get(event.clarify_id)
            if pending is None:
                await self._update_project_card(event, "选择已失效，请重新发起。")
                return
            callback_identity = Identity(event.user_id, event.chat_id, event.chat_type)
            if callback_identity != pending.identity:
                await self._update_project_card(event, "这张项目选择卡片不属于你。")
                return
            display_name = pending.projects.get(event.project_id)
            if display_name is None:
                await self._update_project_card(event, "无效的项目选项。")
                return

            try:
                runtime.select_project_for_session(
                    pending.session_key,
                    callback_identity,
                    event.project_id,
                )
            except Exception as exc:
                await self._update_project_card(event, f"项目选择失败：{exc}")
                return

            self._governor_cards.pop(event.clarify_id, None)
            confirmation = f"已选择项目：{display_name}，正在处理。"
            await self._update_project_card(event, confirmation)
            await super().send(chat_id=event.chat_id, content=confirmation)
            from tools.clarify_gateway import resolve_gateway_clarify

            resolve_gateway_clarify(event.clarify_id, display_name)

        async def _update_project_card(self, event: CardEvent, title: str) -> None:
            try:
                await self._send_reply_request(
                    event.request_id,
                    {
                        "response_type": "update_template_card",
                        "template_card": {
                            "card_type": "text_notice",
                            "main_title": {"title": title},
                            "task_id": event.clarify_id,
                        },
                        "userids": [event.user_id],
                    },
                    cmd=APP_CMD_UPDATE_RESPONSE,
                )
            except Exception:
                # The clarify resolver still receives a valid click even if a
                # client-side card refresh races with a reconnect.
                return

    return GovernedWeComAdapter


def register_governed_wecom_platform(ctx: Any, runtime: GovernorRuntime) -> None:
    from plugins.platforms.wecom.adapter import (
        _is_connected,
        _standalone_send,
        check_wecom_requirements,
        interactive_setup,
    )

    adapter_class = create_governed_adapter_class(runtime)
    ctx.register_platform(
        name="wecom",
        label="WeCom (governed code bot)",
        adapter_factory=lambda config: adapter_class(config),
        check_fn=check_wecom_requirements,
        is_connected=_is_connected,
        validate_config=_is_connected,
        required_env=["WECOM_BOT_ID", "WECOM_SECRET"],
        install_hint="Run `hermes setup` to install WeCom support.",
        setup_fn=interactive_setup,
        allowed_users_env="WECOM_ALLOWED_USERS",
        allow_all_env="WECOM_ALLOW_ALL_USERS",
        cron_deliver_env_var="WECOM_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=4000,
        emoji="🛡️",
        allow_update_command=True,
    )
