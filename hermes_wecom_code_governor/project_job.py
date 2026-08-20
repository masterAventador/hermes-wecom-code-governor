from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .execution import TIMEOUT_EXIT_CODE, combine_output
from .policy import Project
from .sandbox_profile import build_seatbelt_profile
from .seeding import copy_seed, require_safe_seed_paths, seed_workspace

_SAFE_JOB_ID = re.compile(r"^[\w.-]+$")
_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "USER",
        "LOGNAME",
        "SHELL",
        "JAVA_HOME",
        "ANDROID_HOME",
        "ANDROID_SDK_ROOT",
    }
)


@dataclass(frozen=True)
class JobExecutionRequest:
    cwd: Path
    home: Path
    temporary: Path
    argv: tuple[str, ...]
    timeout_seconds: int
    readable_paths: tuple[Path, ...] = ()
    unix_sockets: tuple[Path, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class JobExecutionResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ProjectJobResult:
    status: str
    exit_code: int
    output: str
    base_commit: str
    artifacts: tuple[Path, ...] = ()
    staging_root: Path | None = None


class JobExecutor(Protocol):
    def run(self, request: JobExecutionRequest) -> JobExecutionResult: ...


class CodexSandboxExecutor:
    def __init__(self, codex_binary: Path) -> None:
        self._codex_binary = codex_binary.resolve()

    @staticmethod
    def build_environment(
        request: JobExecutionRequest,
        inherited: Mapping[str, str],
    ) -> dict[str, str]:
        environment = {key: value for key, value in inherited.items() if key in _ENV_ALLOWLIST}
        # 沙箱拒读真实 HOME 下的目录；PATH 里指向 HOME 的条目会让 execvp 型
        # 查找（如 npm 解析 sh）拿到 EPERM 直接失败，进沙箱前必须剔除。
        real_home = Path.home()
        environment["PATH"] = ":".join(
            entry
            for entry in inherited.get("PATH", "").split(":")
            if entry and not Path(entry).is_relative_to(real_home)
        )
        # 项目登记的任务环境变量先并入；随后受管的隔离键再覆盖，
        # 保证配置无法劫持 HOME/TMPDIR 等隔离边界。
        environment.update(dict(request.environment))
        environment.update(
            {
                "HOME": str(request.home.resolve()),
                "TMPDIR": str(request.temporary.resolve()),
                "XDG_CACHE_HOME": str((request.home / ".cache").resolve()),
                "XDG_CONFIG_HOME": str((request.home / ".config").resolve()),
                "npm_config_cache": str((request.home / ".npm").resolve()),
            }
        )
        return environment

    @staticmethod
    def build_sandbox_state(request: JobExecutionRequest) -> dict[str, Any]:
        entries: list[dict[str, Any]] = [
            {
                "path": {"type": "special", "value": {"kind": "minimal"}},
                "access": "read",
            }
        ]
        entries.extend(
            {
                "path": {"type": "path", "path": str(path.resolve())},
                "access": "read",
            }
            for path in request.readable_paths
        )
        entries.extend(
            {
                "path": {"type": "path", "path": str(path.resolve())},
                "access": "write",
            }
            for path in (request.cwd, request.home, request.temporary)
        )
        return {
            "sandboxCwd": request.cwd.resolve().as_uri(),
            "sandboxPolicy": {"type": "read-only"},
            "permissionProfile": {
                "type": "managed",
                "file_system": {"type": "restricted", "entries": entries},
                "network": "restricted",
            },
        }

    def build_command(self, request: JobExecutionRequest) -> tuple[str, ...]:
        state = json.dumps(
            self.build_sandbox_state(request),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        socket_arguments = tuple(
            token
            for path in request.unix_sockets
            for token in ("--allow-unix-socket", str(path.resolve()))
        )
        return (
            str(self._codex_binary),
            "sandbox",
            "--log-denials",
            *socket_arguments,
            "--sandbox-state-json",
            state,
            "--",
            *request.argv,
        )

    def run(self, request: JobExecutionRequest) -> JobExecutionResult:
        return _run_confined(
            self.build_command(request),
            request,
            self.build_environment(request, os.environ),
        )


class SeatbeltGuiExecutor:
    """GUI 类任务执行器：codex sandbox 无法放行 WindowServer 等图形服务，
    这里用共享的 seatbelt profile（默认放行 + 定点收紧）承接需要拉起
    窗口进程的受控任务（如 Electron 静默截图）。"""

    def __init__(
        self,
        denied_read_paths: tuple[Path, ...] = (),
        *,
        allow_network: bool = False,
    ) -> None:
        sandbox_exec = shutil.which("sandbox-exec")
        if sandbox_exec is None:
            raise RuntimeError("sandbox-exec is required for governed GUI jobs")
        self._sandbox_exec = sandbox_exec
        self._denied_read_paths = denied_read_paths
        self._allow_network = allow_network

    def build_command(self, request: JobExecutionRequest) -> tuple[str, ...]:
        profile = build_seatbelt_profile(
            (request.cwd, request.home, request.temporary),
            self._denied_read_paths,
            allow_outbound_network=self._allow_network,
        )
        return (self._sandbox_exec, "-p", profile, *request.argv)

    @staticmethod
    def build_environment(
        request: JobExecutionRequest,
        inherited: Mapping[str, str],
    ) -> dict[str, str]:
        environment = CodexSandboxExecutor.build_environment(request, inherited)
        # 外层 seatbelt 已提供隔离；嵌套沙箱会让 Chromium 自身的沙箱
        # 初始化失败（Operation not permitted），必须显式关闭内层沙箱。
        environment["ELECTRON_DISABLE_SANDBOX"] = "1"
        return environment

    def run(self, request: JobExecutionRequest) -> JobExecutionResult:
        return _run_confined(
            self.build_command(request),
            request,
            self.build_environment(request, os.environ),
        )


def _matches_any(patterns: tuple[tuple[str, ...], ...], argv: tuple[str, ...]) -> bool:
    return any(
        len(pattern) == len(argv)
        and all(
            expected == "*" or expected == actual
            for expected, actual in zip(pattern, argv, strict=True)
        )
        for pattern in patterns
    )


def _run_confined(
    command: tuple[str, ...],
    request: JobExecutionRequest,
    environment: dict[str, str],
) -> JobExecutionResult:
    process = subprocess.Popen(
        command,
        cwd=request.cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=request.timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        timeout_message = f"command timed out after {request.timeout_seconds} seconds"
        stderr = f"{stderr.rstrip()}\n{timeout_message}".strip()
        return JobExecutionResult(TIMEOUT_EXIT_CODE, stdout, stderr)
    return JobExecutionResult(process.returncode, stdout, stderr)


class ProjectJobRunner:
    def __init__(
        self,
        runtime_root: Path,
        *,
        executor: JobExecutor | None = None,
        gui_executor: JobExecutor | None = None,
        network_executor: JobExecutor | None = None,
        codex_binary: Path | None = None,
    ) -> None:
        self.runtime_root = runtime_root.resolve()
        if executor is None:
            if codex_binary is None:
                discovered = shutil.which("codex")
                if discovered is None:
                    raise RuntimeError("codex executable is required for governed project jobs")
                binary = Path(discovered)
            else:
                binary = codex_binary
            executor = CodexSandboxExecutor(binary)
        self._executor = executor
        if gui_executor is None:
            gui_executor = SeatbeltGuiExecutor(
                denied_read_paths=(self.runtime_root.parent / "hermes-home",)
            )
        self._gui_executor = gui_executor
        if network_executor is None:
            # 出网档：签名公证等登记动作需要访问外部服务，写入/密钥约束不变。
            network_executor = SeatbeltGuiExecutor(
                denied_read_paths=(self.runtime_root.parent / "hermes-home",),
                allow_network=True,
            )
        self._network_executor = network_executor

    def run(
        self,
        project: Project,
        *,
        job_id: str,
        argv: tuple[str, ...],
        artifact_globs: tuple[str, ...] = (),
        require_artifacts: bool = True,
    ) -> ProjectJobResult:
        self.validate(project, job_id=job_id, argv=argv, artifact_globs=artifact_globs)
        repo = project.path.resolve()
        self._require_git_repository(repo)
        if self._git(repo, "status", "--porcelain"):
            raise RuntimeError("project checkout must be clean before starting a job")
        base_branch = self._select_base_branch(repo, project.base_branch)
        base_commit = self._git(repo, "rev-parse", base_branch)
        job_root = self.runtime_root / "jobs" / project.project_id / job_id
        workspace = job_root / "workspace"
        home = job_root / "home"
        temporary = job_root / "tmp"
        if job_root.exists():
            raise RuntimeError(f"job path already exists: {job_root}")

        workspace.parent.mkdir(parents=True, exist_ok=True)
        self._git(repo, "worktree", "add", "--detach", str(workspace), base_branch)
        home.mkdir(parents=True)
        temporary.mkdir(parents=True)
        if _matches_any(project.job_network_commands, argv):
            executor = self._network_executor
        elif _matches_any(project.job_gui_commands, argv):
            executor = self._gui_executor
        else:
            executor = self._executor
        try:
            self._seed_job(project, workspace, home)
            execution = executor.run(
                JobExecutionRequest(
                    cwd=workspace,
                    home=home,
                    temporary=temporary,
                    argv=argv,
                    timeout_seconds=project.job_timeout_seconds,
                    readable_paths=project.readable_paths,
                    unix_sockets=project.job_unix_sockets,
                    environment=self._resolved_environment(project, workspace, home),
                )
            )
            output = combine_output(execution.stdout, execution.stderr)
            if execution.exit_code != 0:
                return ProjectJobResult(
                    status="failed",
                    exit_code=execution.exit_code,
                    output=output,
                    base_commit=base_commit,
                )
            artifacts = self._stage_artifacts(
                workspace,
                job_id,
                artifact_globs,
                require_artifacts=require_artifacts,
            )
            return ProjectJobResult(
                status="completed",
                exit_code=0,
                output=output,
                base_commit=base_commit,
                artifacts=artifacts,
                staging_root=(self.runtime_root / "artifacts" / job_id if artifacts else None),
            )
        finally:
            self._remove_worktree(repo, workspace)
            shutil.rmtree(job_root, ignore_errors=True)

    @staticmethod
    def _resolved_environment(
        project: Project,
        workspace: Path,
        home: Path,
    ) -> tuple[tuple[str, str], ...]:
        return tuple(
            (
                name,
                template.replace("${JOB_HOME}", str(home.resolve())).replace(
                    "${WORKSPACE}", str(workspace.resolve())
                ),
            )
            for name, template in project.job_environment
        )

    def _stage_artifacts(
        self,
        workspace: Path,
        job_id: str,
        artifact_globs: tuple[str, ...],
        *,
        require_artifacts: bool = True,
    ) -> tuple[Path, ...]:
        if not artifact_globs:
            return ()
        staging_root = self.runtime_root / "artifacts" / job_id
        if staging_root.exists():
            raise RuntimeError(f"artifact staging path already exists: {staging_root}")
        staged: list[Path] = []
        for pattern in artifact_globs:
            for candidate in sorted(workspace.glob(pattern)):
                if candidate.is_symlink():
                    raise PermissionError("artifact symlinks are not allowed")
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(workspace.resolve()) or not resolved.is_file():
                    raise PermissionError(
                        "artifact must be a regular file inside the job workspace"
                    )
                relative = resolved.relative_to(workspace.resolve())
                destination = staging_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(resolved, destination)
                staged.append(destination)
        if not staged:
            # 回退来的 glob（require_artifacts=False）允许任务本身不产出该类产物，
            # 例如登记了打包产物的项目跑纯测试命令；显式请求的 glob 仍严格要求产出。
            if require_artifacts:
                raise FileNotFoundError("configured job artifact was not produced")
            return ()
        return tuple(staged)

    @staticmethod
    def validate(
        project: Project,
        *,
        job_id: str,
        argv: tuple[str, ...],
        artifact_globs: tuple[str, ...] = (),
    ) -> None:
        if not _SAFE_JOB_ID.fullmatch(job_id):
            raise ValueError("job id may only contain letters, numbers, dot, dash and underscore")
        if not argv or not all(isinstance(token, str) and token for token in argv):
            raise ValueError("job command must be a non-empty argv")
        if not _matches_any(project.job_allowed_commands, argv):
            raise PermissionError("command is not allowed for this project")
        allowed_globs = set(project.job_artifact_globs)
        for pattern in artifact_globs:
            pure = PurePosixPath(pattern)
            if (
                pattern not in allowed_globs
                or pure.is_absolute()
                or ".." in pure.parts
                or ".git" in pure.parts
            ):
                raise PermissionError(
                    "artifact glob is not allowed for this project; allowed: "
                    + (", ".join(sorted(allowed_globs)) or "none")
                )
        require_safe_seed_paths(project.seed_paths)
        for _, target in project.job_home_seeds:
            if target.is_absolute() or ".." in target.parts:
                raise PermissionError("home seed target must stay inside the isolated home")

    @staticmethod
    def _seed_job(project: Project, workspace: Path, home: Path) -> None:
        seed_workspace(project.path, workspace, project.seed_paths)
        for source_path, relative_target in project.job_home_seeds:
            source = source_path.resolve(strict=True)
            destination = (home / relative_target).resolve()
            if not destination.is_relative_to(home.resolve()):
                raise PermissionError("home seed target must stay inside the isolated home")
            copy_seed(source, destination)

    def _select_base_branch(self, repo: Path, configured: str | None) -> str:
        if configured is not None:
            if not self._ref_exists(repo, f"refs/heads/{configured}"):
                raise RuntimeError(f"configured base branch does not exist: {configured}")
            return configured
        for candidate in ("dev", "main", "master"):
            if self._ref_exists(repo, f"refs/heads/{candidate}"):
                return candidate
        raise RuntimeError("no supported base branch found (dev, main or master)")

    def _require_git_repository(self, repo: Path) -> None:
        result = self._run(repo, "git", "rev-parse", "--is-inside-work-tree")
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise RuntimeError(f"project is not a Git repository: {repo}")

    def _ref_exists(self, repo: Path, ref: str) -> bool:
        return self._run(repo, "git", "show-ref", "--verify", "--quiet", ref).returncode == 0

    def _remove_worktree(self, repo: Path, workspace: Path) -> None:
        result = self._run(repo, "git", "worktree", "remove", "--force", str(workspace))
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"job worktree cleanup failed: {detail}")

    def _git(self, cwd: Path, *args: str) -> str:
        result = self._run(cwd, "git", *args)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"git failed ({' '.join(args)}): {detail}")
        return result.stdout.strip()

    @staticmethod
    def _run(cwd: Path, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
