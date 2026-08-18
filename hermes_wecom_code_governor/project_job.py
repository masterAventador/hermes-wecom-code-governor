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

from .policy import Project

_SAFE_JOB_ID = re.compile(r"^[\w.-]+$")
_MAX_JOB_OUTPUT_CHARS = 12_000
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
        environment = self.build_environment(request, os.environ)
        command = self.build_command(request)
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
            return JobExecutionResult(124, stdout, stderr)
        return JobExecutionResult(process.returncode, stdout, stderr)


class ProjectJobRunner:
    def __init__(
        self,
        runtime_root: Path,
        *,
        executor: JobExecutor | None = None,
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

    def run(
        self,
        project: Project,
        *,
        job_id: str,
        argv: tuple[str, ...],
        artifact_globs: tuple[str, ...] = (),
    ) -> ProjectJobResult:
        self._validate_request(project, job_id, argv, artifact_globs)
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
        try:
            self._seed_job(project, workspace, home)
            execution = self._executor.run(
                JobExecutionRequest(
                    cwd=workspace,
                    home=home,
                    temporary=temporary,
                    argv=argv,
                    timeout_seconds=project.job_timeout_seconds,
                    readable_paths=project.job_readable_paths,
                    unix_sockets=project.job_unix_sockets,
                )
            )
            output = self._combined_output(execution)
            if execution.exit_code != 0:
                return ProjectJobResult(
                    status="failed",
                    exit_code=execution.exit_code,
                    output=output,
                    base_commit=base_commit,
                )
            artifacts = self._stage_artifacts(workspace, job_id, artifact_globs)
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
    def _combined_output(result: JobExecutionResult) -> str:
        output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        if len(output) <= _MAX_JOB_OUTPUT_CHARS:
            return output
        omitted = len(output) - _MAX_JOB_OUTPUT_CHARS
        return f"[前面 {omitted} 个字符已省略]\n{output[-_MAX_JOB_OUTPUT_CHARS:]}"

    def _stage_artifacts(
        self,
        workspace: Path,
        job_id: str,
        artifact_globs: tuple[str, ...],
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
            raise FileNotFoundError("configured job artifact was not produced")
        return tuple(staged)

    @staticmethod
    def _validate_request(
        project: Project,
        job_id: str,
        argv: tuple[str, ...],
        artifact_globs: tuple[str, ...],
    ) -> None:
        if not _SAFE_JOB_ID.fullmatch(job_id):
            raise ValueError("job id may only contain letters, numbers, dot, dash and underscore")
        if not argv or not all(isinstance(token, str) and token for token in argv):
            raise ValueError("job command must be a non-empty argv")
        allowed = any(
            len(pattern) == len(argv)
            and all(
                expected == "*" or expected == actual
                for expected, actual in zip(pattern, argv, strict=True)
            )
            for pattern in project.job_allowed_commands
        )
        if not allowed:
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
                raise PermissionError("artifact glob is not allowed for this project")
        for seed_path in project.job_seed_paths:
            pure = PurePosixPath(seed_path)
            if pure.is_absolute() or ".." in pure.parts or ".git" in pure.parts:
                raise PermissionError("project seed path must stay inside the repository")
        for _, target in project.job_home_seeds:
            if target.is_absolute() or ".." in target.parts:
                raise PermissionError("home seed target must stay inside the isolated home")

    def _seed_job(self, project: Project, workspace: Path, home: Path) -> None:
        repo = project.path.resolve()
        for relative in project.job_seed_paths:
            source = (repo / relative).resolve(strict=True)
            if not source.is_relative_to(repo):
                raise PermissionError("project seed path must stay inside the repository")
            destination = workspace / relative
            self._copy_seed(source, destination)
        for source_path, relative_target in project.job_home_seeds:
            source = source_path.resolve(strict=True)
            destination = (home / relative_target).resolve()
            if not destination.is_relative_to(home.resolve()):
                raise PermissionError("home seed target must stay inside the isolated home")
            self._copy_seed(source, destination)

    @staticmethod
    def _copy_seed(source: Path, destination: Path) -> None:
        if destination.exists():
            raise RuntimeError(f"job seed destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
            return
        if source.is_file():
            shutil.copy2(source, destination)
            return
        raise ValueError(f"job seed must be a file or directory: {source}")

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
