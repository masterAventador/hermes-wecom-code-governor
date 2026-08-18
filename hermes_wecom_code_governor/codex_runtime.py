from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class CodexMode(Enum):
    WRITE = "write"


class CodexTaskState(Enum):
    COMPLETED = "completed"
    NEEDS_INPUT = "needs_input"


@dataclass(frozen=True)
class CodexRuntimeSettings:
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "xhigh"
    codex_bin: Path | None = None


@dataclass(frozen=True)
class CodexRunRequest:
    mode: CodexMode
    prompt: str
    cwd: Path
    thread_id: str | None = None
    readable_roots: tuple[Path, ...] = ()
    writable_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class CodexRunResult:
    thread_id: str
    answer: str
    task_state: CodexTaskState | None = None


class CodexRunner(Protocol):
    def run(self, request: CodexRunRequest) -> CodexRunResult: ...


class _AppServerClient(Protocol):
    def start(self) -> None: ...

    def initialize(self) -> object: ...

    def thread_start(self, params: dict) -> object: ...

    def thread_resume(self, thread_id: str, params: dict) -> object: ...

    def turn_start(self, thread_id: str, prompt: str, params: dict) -> object: ...

    def close(self) -> None: ...


class CodexAppServerRunner:
    """Run coding turns through Codex's native app-server agent harness."""

    SERVICE_NAME = "hermes-wecom-code-governor"
    PERMISSION_PROFILE = "hermes-governor"
    PERMISSION_DESCRIPTION = "Hermes 企微代码机器人单次受控项目权限"
    WRITE_INSTRUCTIONS = (
        "你是受管控代码机器人的代码修改执行器。只在当前 worktree 内完成用户明确要求的"
        "最小必要修改并运行相关检查。不得执行 git commit、merge、push、worktree、rebase；"
        "不得打包、上传、部署或访问授权根目录之外的文件；不得大范围删除。"
        "如果信息不足，不要留下半成品修改，回答需要用户补充的具体问题，并把 task_state"
        "设为 needs_input；只有修改与自测都完成时才设为 completed。"
    )
    _WRITE_OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "task_state": {
                "type": "string",
                "enum": [state.value for state in CodexTaskState],
            },
        },
        "required": ["answer", "task_state"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        settings: CodexRuntimeSettings,
        *,
        client_factory: Callable[[object], _AppServerClient] | None = None,
        result_collector: Callable[[_AppServerClient, str, str], object] | None = None,
    ) -> None:
        self.settings = settings
        self._client_factory = client_factory or self._default_client_factory
        self._result_collector = result_collector or self._default_result_collector

    def run(self, request: CodexRunRequest) -> CodexRunResult:
        prompt = request.prompt.strip()
        if not prompt:
            raise ValueError("Codex request must not be empty")
        cwd = request.cwd.resolve()
        if not cwd.is_dir():
            raise RuntimeError(f"Codex working directory does not exist: {cwd}")

        client = self._client_factory(self._launch_config())
        client.start()
        try:
            client.initialize()
            lifecycle = {
                "approvalPolicy": "never",
                "config": self._permission_config(request),
                "cwd": str(cwd),
                "developerInstructions": self.WRITE_INSTRUCTIONS,
                "model": self.settings.model,
                "permissions": self.PERMISSION_PROFILE,
            }
            if request.thread_id:
                resumed = client.thread_resume(request.thread_id, lifecycle)
                thread_id = str(resumed.thread.id)
            else:
                started = client.thread_start({**lifecycle, "serviceName": self.SERVICE_NAME})
                thread_id = str(started.thread.id)

            turn_params = {
                "approvalPolicy": "never",
                "cwd": str(cwd),
                "effort": self.settings.reasoning_effort,
                "model": self.settings.model,
                "outputSchema": self._WRITE_OUTPUT_SCHEMA,
            }
            turn = client.turn_start(thread_id, prompt, turn_params)
            result = self._result_collector(client, thread_id, str(turn.turn.id))
            return self._parse_result(thread_id, result)
        finally:
            client.close()

    def _launch_config(self) -> object:
        from openai_codex import CodexConfig

        codex_bin = self.settings.codex_bin
        if codex_bin is None:
            import shutil

            resolved = shutil.which("codex")
            if resolved is None:
                raise RuntimeError("Codex CLI is not installed or not on PATH")
            codex_bin = Path(resolved)
        codex_bin = codex_bin.expanduser().resolve()
        if not codex_bin.is_file():
            raise RuntimeError(f"Codex CLI does not exist: {codex_bin}")

        launch = ["/usr/bin/env", "-i"]
        for key in (
            "HOME",
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "USER",
            "LOGNAME",
            "SHELL",
            "TMPDIR",
            "CODEX_HOME",
        ):
            value = os.environ.get(key)
            if value:
                launch.append(f"{key}={value}")
        launch.extend((str(codex_bin), "app-server", "--listen", "stdio://"))
        return CodexConfig(launch_args_override=tuple(launch), experimental_api=True)

    @staticmethod
    def _default_client_factory(config: object) -> _AppServerClient:
        from openai_codex.client import CodexClient

        def deny_approval(_method: str, _params: object) -> dict[str, str]:
            return {"decision": "decline"}

        return CodexClient(config=config, approval_handler=deny_approval)

    @staticmethod
    def _default_result_collector(client: _AppServerClient, thread_id: str, turn_id: str) -> object:
        from openai_codex import TurnHandle

        return TurnHandle(client, thread_id, turn_id).run()

    @staticmethod
    def _resolved_roots(paths: tuple[Path, ...], fallback: Path) -> list[str]:
        roots = paths or (fallback,)
        return list(dict.fromkeys(str(path.resolve()) for path in roots))

    def _permission_config(self, request: CodexRunRequest) -> dict[str, Any]:
        cwd = request.cwd.resolve()
        filesystem: dict[str, Any] = {
            ":minimal": "read",
            ":workspace_roots": {
                ".": "write",
                "**/.env": "deny",
                "**/.env.*": "deny",
            },
        }
        for root in self._resolved_roots(request.readable_roots, cwd):
            filesystem[root] = "read"
        writable_roots = self._resolved_roots(request.writable_roots, cwd)
        for root in writable_roots:
            filesystem[root] = "write"
        return {
            "default_permissions": self.PERMISSION_PROFILE,
            "permissions": {
                self.PERMISSION_PROFILE: {
                    "description": self.PERMISSION_DESCRIPTION,
                    "filesystem": filesystem,
                    "network": {"enabled": False},
                    "workspace_roots": {root: True for root in writable_roots},
                }
            },
        }

    @staticmethod
    def _parse_result(thread_id: str, result: object) -> CodexRunResult:
        raw = getattr(result, "final_response", None)
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError("Codex returned no final response")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Codex returned an invalid structured response") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Codex structured response must be an object")
        answer = payload.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("Codex structured response is missing answer")
        task_state = payload.get("task_state")
        try:
            state = CodexTaskState(task_state)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Codex structured response has invalid task_state") from exc
        return CodexRunResult(thread_id, answer.strip(), state)
