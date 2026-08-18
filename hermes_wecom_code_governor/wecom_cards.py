from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_RECOMMENDED = "(Recommended)"
_MAX_PROJECT_CARD_CHOICES = 6


@dataclass(frozen=True)
class CardEvent:
    request_id: str
    clarify_id: str
    project_id: str
    user_id: str
    chat_id: str
    chat_type: str


def _choice_text(choice: object) -> str:
    text = str(choice).strip()
    if text.casefold().endswith(_RECOMMENDED.casefold()):
        text = text[: -len(_RECOMMENDED)].strip()
    return text


def match_project_choices(
    choices: list | None,
    projects: dict[str, str],
) -> tuple[tuple[str, str], ...] | None:
    if not choices or not projects or len(choices) > _MAX_PROJECT_CARD_CHOICES:
        return None
    normalized = [_choice_text(choice) for choice in choices]
    if len(set(normalized)) != len(normalized):
        return None
    by_name = {display_name: project_id for project_id, display_name in projects.items()}
    if not set(normalized).issubset(by_name):
        return None
    return tuple((by_name[display_name], display_name) for display_name in normalized)


def parse_card_event(payload: dict[str, Any]) -> CardEvent | None:
    if payload.get("cmd") != "aibot_event_callback":
        return None
    headers = payload.get("headers")
    body = payload.get("body")
    if not isinstance(headers, dict) or not isinstance(body, dict):
        return None
    event = body.get("event")
    if not isinstance(event, dict):
        return None
    nested = event.get("template_card_event")
    card = nested if isinstance(nested, dict) else event
    sender = body.get("from")
    if not isinstance(sender, dict):
        return None

    request_id = str(headers.get("req_id") or "").strip()
    clarify_id = str(card.get("task_id") or "").strip()
    project_id = str(card.get("event_key") or "").strip()
    user_id = str(sender.get("userid") or "").strip()
    chat_id = str(body.get("chatid") or user_id).strip()
    if not all((request_id, clarify_id, project_id, user_id, chat_id)):
        return None
    chat_type = str(body.get("chattype") or ("dm" if chat_id == user_id else "group")).lower()
    return CardEvent(
        request_id=request_id,
        clarify_id=clarify_id,
        project_id=project_id,
        user_id=user_id,
        chat_id=chat_id,
        chat_type=chat_type,
    )
