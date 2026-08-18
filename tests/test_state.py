from dataclasses import fields
from pathlib import Path

from hermes_wecom_code_governor.policy import Identity, Project
from hermes_wecom_code_governor.state import SessionRecord, SessionStateRepository
from hermes_wecom_code_governor.worktree import ActiveWorktree


def test_session_record_only_persists_active_code_change_thread() -> None:
    assert [field.name for field in fields(SessionRecord)] == [
        "identity",
        "project_id",
        "active_worktree",
        "active_task_thread_id",
    ]


class MemoryState:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def set(self, key: str, value: object) -> None:
        self.values[key] = value


def test_session_state_survives_repository_recreation() -> None:
    state = MemoryState()
    project = Project("demo", "Demo", Path("/workspace/demo"))
    active = ActiveWorktree(
        project=project,
        task_id="0817-update-readme",
        path=Path("/runtime/demo/0817-update-readme"),
        branch_name="bot/0817-update-readme",
        base_branch="dev",
        base_commit="a" * 40,
    )
    record = SessionRecord(
        identity=Identity("user-1", "chat-1", "group"),
        project_id="demo",
        active_worktree=active,
        active_task_thread_id="write-thread",
    )

    SessionStateRepository(state, {"demo": project}).save(
        "agent:main:wecom:group:chat-1:user-1", record
    )
    restored = SessionStateRepository(state, {"demo": project}).load(
        "agent:main:wecom:group:chat-1:user-1"
    )

    assert restored == record
    assert len(state.values) == 1
    assert next(iter(state.values)).startswith("session:")


def test_obsolete_read_thread_state_is_ignored_for_old_records() -> None:
    state = MemoryState()
    project = Project("demo", "Demo", Path("/workspace/demo"))
    repository = SessionStateRepository(state, {"demo": project})
    key = repository.storage_key("session")
    state.values[key] = {
        "identity": {"user_id": "u", "chat_id": "c", "chat_type": "group"},
        "project_id": "demo",
        "active_worktree": None,
        "project_thread_ids": {"demo": "obsolete-read-thread"},
    }

    restored = repository.load("session")

    assert restored.active_task_thread_id is None


def test_malformed_or_unknown_project_state_fails_closed() -> None:
    state = MemoryState()
    repository = SessionStateRepository(state, {})
    key = repository.storage_key("session")
    state.values[key] = {
        "identity": {"user_id": "u", "chat_id": "c", "chat_type": "group"},
        "project_id": "removed-project",
    }

    assert repository.load("session") is None
