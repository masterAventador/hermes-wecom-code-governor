from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_wecom_code_governor.codex_runtime import (
    CodexAppServerRunner,
    CodexMode,
    CodexRunRequest,
    CodexRuntimeSettings,
    CodexTaskState,
)


def test_native_codex_runtime_only_supports_code_changes() -> None:
    assert [mode.value for mode in CodexMode] == ["write"]


class FakeClient:
    def __init__(self, config: object, final_response: str) -> None:
        self.config = config
        self.final_response = final_response
        self.started = False
        self.closed = False
        self.thread_start_params: dict | None = None
        self.thread_resume_call: tuple[str, dict] | None = None
        self.turn_start_call: tuple[str, str, dict] | None = None

    def start(self) -> None:
        self.started = True

    def initialize(self) -> object:
        return object()

    def thread_start(self, params: dict) -> object:
        self.thread_start_params = params
        return SimpleNamespace(thread=SimpleNamespace(id="thread-new"))

    def thread_resume(self, thread_id: str, params: dict) -> object:
        self.thread_resume_call = (thread_id, params)
        return SimpleNamespace(thread=SimpleNamespace(id=thread_id))

    def turn_start(self, thread_id: str, prompt: str, params: dict) -> object:
        self.turn_start_call = (thread_id, prompt, params)
        return SimpleNamespace(turn=SimpleNamespace(id="turn-1"))

    def close(self) -> None:
        self.closed = True


def make_runner(
    final: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> tuple[CodexAppServerRunner, list[FakeClient]]:
    monkeypatch.setenv("WECOM_SECRET", "must-not-reach-codex")
    monkeypatch.setenv("COS_SECRET_KEY", "must-not-reach-codex")
    clients: list[FakeClient] = []

    def client_factory(config: object) -> FakeClient:
        client = FakeClient(config, json.dumps(final, ensure_ascii=False))
        clients.append(client)
        return client

    runner = CodexAppServerRunner(
        CodexRuntimeSettings(
            model="gpt-5.6-sol",
            reasoning_effort="xhigh",
            codex_bin=Path("/opt/homebrew/bin/codex"),
        ),
        client_factory=client_factory,
        result_collector=lambda client, _thread_id, _turn_id: SimpleNamespace(
            final_response=client.final_response
        ),
    )
    return runner, clients


def test_write_turn_uses_native_codex_thread_with_restricted_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, clients = make_runner({"answer": "已经修复。", "task_state": "completed"}, monkeypatch)
    worktree = tmp_path / "worktree"
    git_admin = tmp_path / "repo" / ".git" / "worktrees" / "task"
    worktree.mkdir()
    git_admin.mkdir(parents=True)

    result = runner.run(
        CodexRunRequest(
            mode=CodexMode.WRITE,
            prompt="修复登录问题",
            cwd=worktree,
            readable_roots=(worktree, tmp_path / "repo" / ".git"),
            writable_roots=(worktree, git_admin),
        )
    )

    assert result.thread_id == "thread-new"
    assert result.answer == "已经修复。"
    assert result.task_state is CodexTaskState.COMPLETED
    client = clients[0]
    assert client.started and client.closed
    assert client.thread_start_params == {
        "approvalPolicy": "never",
        "config": {
            "default_permissions": "hermes-governor",
            "permissions": {
                "hermes-governor": {
                    "description": "Hermes 企微代码机器人单次受控项目权限",
                    "filesystem": {
                        ":minimal": "read",
                        ":workspace_roots": {
                            ".": "write",
                            "**/.env": "deny",
                            "**/.env.*": "deny",
                        },
                        str(worktree.resolve()): "write",
                        str((tmp_path / "repo" / ".git").resolve()): "read",
                        str(git_admin.resolve()): "write",
                    },
                    "network": {"enabled": False, "allowLocalBinding": True},
                    "workspace_roots": {
                        str(worktree.resolve()): True,
                        str(git_admin.resolve()): True,
                    },
                }
            },
        },
        "cwd": str(worktree.resolve()),
        "developerInstructions": runner.WRITE_INSTRUCTIONS,
        "model": "gpt-5.6-sol",
        "permissions": "hermes-governor",
        "serviceName": "hermes-wecom-code-governor",
    }
    thread_id, prompt, turn = client.turn_start_call
    assert thread_id == "thread-new"
    assert prompt == "修复登录问题"
    assert turn["approvalPolicy"] == "never"
    assert turn["effort"] == "xhigh"
    assert turn["model"] == "gpt-5.6-sol"
    assert "sandboxPolicy" not in turn
    assert turn["outputSchema"]["required"] == ["answer", "task_state"]

    launch_args = client.config.launch_args_override
    assert launch_args[:2] == ("/usr/bin/env", "-i")
    assert "WECOM_SECRET=must-not-reach-codex" not in launch_args
    assert "COS_SECRET_KEY=must-not-reach-codex" not in launch_args
    assert launch_args[-4:] == (
        str(Path("/opt/homebrew/bin/codex").resolve()),
        "app-server",
        "--listen",
        "stdio://",
    )


def test_invalid_structured_result_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = make_runner({"answer": "改了一半"}, monkeypatch)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with pytest.raises(RuntimeError, match="task_state"):
        runner.run(
            CodexRunRequest(
                mode=CodexMode.WRITE,
                prompt="修改代码",
                cwd=worktree,
                readable_roots=(worktree,),
                writable_roots=(worktree,),
            )
        )
