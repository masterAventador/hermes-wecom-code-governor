from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from .policy import Identity, Project
from .worktree import ActiveWorktree


class DurableState(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...


@dataclass(frozen=True)
class SessionRecord:
    identity: Identity
    project_id: str | None = None
    active_worktree: ActiveWorktree | None = None
    active_task_thread_id: str | None = None


class SessionStateRepository:
    def __init__(self, state: DurableState, projects: dict[str, Project]) -> None:
        self._state = state
        self._projects = projects

    @staticmethod
    def storage_key(session_key: str) -> str:
        digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
        return f"session:{digest}"

    def load(self, session_key: str) -> SessionRecord | None:
        raw = self._state.get(self.storage_key(session_key))
        if not isinstance(raw, dict):
            return None
        try:
            identity_data = raw["identity"]
            if not isinstance(identity_data, dict):
                return None
            identity = Identity(
                user_id=self._nonempty(identity_data["user_id"]),
                chat_id=self._nonempty(identity_data["chat_id"]),
                chat_type=self._nonempty(identity_data["chat_type"]),
            )
            project_id = raw.get("project_id")
            if project_id is not None:
                project_id = self._nonempty(project_id)
                if project_id not in self._projects:
                    return None

            active_data = raw.get("active_worktree")
            active = None
            if active_data is not None:
                if not isinstance(active_data, dict):
                    return None
                active_project_id = self._nonempty(active_data["project_id"])
                project = self._projects.get(active_project_id)
                if project is None or project_id != active_project_id:
                    return None
                active = ActiveWorktree(
                    project=project,
                    task_id=self._nonempty(active_data["task_id"]),
                    path=project.path.__class__(self._nonempty(active_data["path"])),
                    branch_name=self._nonempty(active_data["branch_name"]),
                    base_branch=self._nonempty(active_data["base_branch"]),
                    base_commit=self._nonempty(active_data["base_commit"]),
                )
            active_task_thread_id = raw.get("active_task_thread_id")
            if active_task_thread_id is not None:
                active_task_thread_id = self._nonempty(active_task_thread_id)
                if active is None:
                    return None
            return SessionRecord(
                identity,
                project_id,
                active,
                active_task_thread_id,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def save(self, session_key: str, record: SessionRecord) -> None:
        active = record.active_worktree
        payload: dict[str, Any] = {
            "identity": {
                "user_id": record.identity.user_id,
                "chat_id": record.identity.chat_id,
                "chat_type": record.identity.chat_type,
            },
            "project_id": record.project_id,
            "active_worktree": None,
            "active_task_thread_id": record.active_task_thread_id,
        }
        if active is not None:
            payload["active_worktree"] = {
                "project_id": active.project.project_id,
                "task_id": active.task_id,
                "path": str(active.path),
                "branch_name": active.branch_name,
                "base_branch": active.base_branch,
                "base_commit": active.base_commit,
            }
        self._state.set(self.storage_key(session_key), payload)

    @staticmethod
    def _nonempty(value: object) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("state value must be a non-empty string")
        return value
