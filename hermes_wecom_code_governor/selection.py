from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from threading import RLock

from .policy import Identity


class SelectionStatus(Enum):
    SELECTED = "selected"
    FORBIDDEN = "forbidden"
    INVALID = "invalid"
    EXPIRED = "expired"


@dataclass(frozen=True)
class CardButton:
    text: str
    key: str


@dataclass(frozen=True)
class SelectionResult:
    status: SelectionStatus
    session_key: str = ""
    project_id: str | None = None
    response_text: str | None = None


@dataclass(frozen=True)
class _PendingCard:
    session_key: str
    identity: Identity
    projects: dict[str, str]
    created_at: float


@dataclass(frozen=True)
class _SelectedProject:
    identity: Identity
    project_id: str


class ProjectSelectionService:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = 300,
    ) -> None:
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._pending: dict[str, _PendingCard] = {}
        self._selected: dict[str, _SelectedProject] = {}
        self._lock = RLock()

    def register_card(
        self,
        clarify_id: str,
        session_key: str,
        identity: Identity,
        projects: Mapping[str, str],
    ) -> tuple[CardButton, ...]:
        pending = _PendingCard(
            session_key=session_key,
            identity=identity,
            projects=dict(projects),
            created_at=self._clock(),
        )
        with self._lock:
            self._pending[clarify_id] = pending
        return tuple(CardButton(text=name, key=project_id) for project_id, name in projects.items())

    def resolve_card(
        self,
        clarify_id: str,
        identity: Identity,
        project_id: str,
    ) -> SelectionResult:
        with self._lock:
            pending = self._pending.get(clarify_id)
            if pending is None:
                return SelectionResult(SelectionStatus.EXPIRED)
            if self._clock() - pending.created_at > self._ttl_seconds:
                self._pending.pop(clarify_id, None)
                return SelectionResult(SelectionStatus.EXPIRED)
            if identity != pending.identity:
                return SelectionResult(SelectionStatus.FORBIDDEN, session_key=pending.session_key)
            display_name = pending.projects.get(project_id)
            if display_name is None:
                return SelectionResult(SelectionStatus.INVALID, session_key=pending.session_key)
            self._pending.pop(clarify_id, None)
            self._selected[pending.session_key] = _SelectedProject(identity, project_id)
            return SelectionResult(
                SelectionStatus.SELECTED,
                session_key=pending.session_key,
                project_id=project_id,
                response_text=display_name,
            )

    def select_explicitly(
        self,
        session_key: str,
        identity: Identity,
        project_id: str,
        authorized_project_ids: Iterable[str],
    ) -> None:
        if project_id not in frozenset(authorized_project_ids):
            raise PermissionError(f"project {project_id!r} is not authorized")
        with self._lock:
            self._selected[session_key] = _SelectedProject(identity, project_id)

    def current_project_id(self, session_key: str, identity: Identity) -> str | None:
        with self._lock:
            selected = self._selected.get(session_key)
            if selected is None or selected.identity != identity:
                return None
            return selected.project_id
