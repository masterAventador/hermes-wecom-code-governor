from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .policy import Project

_SAFE_TASK_ID = re.compile(r"^[\w.-]+$")


class CompletionStatus(Enum):
    MERGED = "merged"
    NO_CHANGES = "no_changes"
    VALIDATION_FAILED = "validation_failed"
    SAFETY_BLOCKED = "safety_blocked"
    MERGE_FAILED = "merge_failed"


@dataclass(frozen=True)
class SafetyLimits:
    max_changed_files: int
    max_deleted_files: int


@dataclass(frozen=True)
class ActiveWorktree:
    project: Project
    task_id: str
    path: Path
    branch_name: str
    base_branch: str
    base_commit: str


@dataclass(frozen=True)
class CompletionResult:
    status: CompletionStatus
    message: str = ""
    commit: str | None = None


class WorktreeManager:
    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root.resolve()

    def begin(self, project: Project, task_id: str) -> ActiveWorktree:
        if not _SAFE_TASK_ID.fullmatch(task_id):
            raise ValueError("task id may only contain letters, numbers, dot, dash and underscore")

        repo = project.path.resolve()
        self._require_git_repository(repo)
        base_branch = self._select_base_branch(repo, project.base_branch)
        current_branch = self._git(repo, "branch", "--show-current")
        if current_branch != base_branch:
            raise RuntimeError(
                f"base checkout must be on {base_branch!r}; current branch is {current_branch!r}"
            )
        if self._git(repo, "status", "--porcelain"):
            raise RuntimeError("base checkout must be clean before creating a worktree")

        base_commit = self._git(repo, "rev-parse", base_branch)
        branch_name = f"bot/{task_id}"
        if self._ref_exists(repo, f"refs/heads/{branch_name}"):
            raise RuntimeError(f"task branch already exists: {branch_name}")

        worktree_path = self.runtime_root / project.project_id / task_id
        if worktree_path.exists():
            raise RuntimeError(f"worktree path already exists: {worktree_path}")

        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            repo,
            "worktree",
            "add",
            "-b",
            branch_name,
            str(worktree_path),
            base_branch,
        )
        return ActiveWorktree(
            project=project,
            task_id=task_id,
            path=worktree_path,
            branch_name=branch_name,
            base_branch=base_branch,
            base_commit=base_commit,
        )

    def complete(
        self,
        active: ActiveWorktree,
        limits: SafetyLimits,
    ) -> CompletionResult:
        changed_files, deleted_files = self._change_counts(active.path)
        safety_error = self._safety_result(changed_files, deleted_files, limits)
        if safety_error is not None:
            return safety_error
        if changed_files == 0:
            self._cleanup(active)
            return CompletionResult(CompletionStatus.NO_CHANGES, "no file changes")

        validation = self._run_validation(active)
        if validation is not None:
            return CompletionResult(CompletionStatus.VALIDATION_FAILED, validation)
        changed_files, deleted_files = self._change_counts(active.path)
        safety_error = self._safety_result(changed_files, deleted_files, limits)
        if safety_error is not None:
            return safety_error

        precondition_error = self._check_merge_preconditions(active)
        if precondition_error is not None:
            return CompletionResult(CompletionStatus.MERGE_FAILED, precondition_error)

        commit_message = f"机器人：{active.task_id}"
        commit = self._commit(active, commit_message)
        merge = self._run(
            active.project.path.resolve(),
            "git",
            "merge",
            "--ff-only",
            active.branch_name,
        )
        if merge.returncode != 0:
            return CompletionResult(
                CompletionStatus.MERGE_FAILED,
                self._format_failure("fast-forward merge", merge),
                commit[:7],
            )

        self._cleanup(active)
        return CompletionResult(CompletionStatus.MERGED, commit=commit[:7])

    def codex_roots(self, active: ActiveWorktree) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        worktree = active.path.resolve()
        repository = active.project.path.resolve()
        common_git_value = Path(self._git(repository, "rev-parse", "--git-common-dir"))
        if not common_git_value.is_absolute():
            common_git_value = repository / common_git_value
        common_git_dir = common_git_value.resolve()
        git_admin = Path(self._git(worktree, "rev-parse", "--absolute-git-dir")).resolve()
        expected_admin_root = common_git_dir / "worktrees"
        if not git_admin.is_relative_to(expected_admin_root):
            raise RuntimeError(f"unexpected worktree Git directory: {git_admin}")
        return (worktree, common_git_dir), (worktree, git_admin)

    @staticmethod
    def _safety_result(
        changed_files: int,
        deleted_files: int,
        limits: SafetyLimits,
    ) -> CompletionResult | None:
        if changed_files > limits.max_changed_files:
            return CompletionResult(
                CompletionStatus.SAFETY_BLOCKED,
                f"changed files {changed_files} exceeds limit {limits.max_changed_files}",
            )
        if deleted_files > limits.max_deleted_files:
            return CompletionResult(
                CompletionStatus.SAFETY_BLOCKED,
                f"deleted files {deleted_files} exceeds limit {limits.max_deleted_files}",
            )
        return None

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
        result = self._run(repo, "git", "show-ref", "--verify", "--quiet", ref)
        return result.returncode == 0

    def _change_counts(self, worktree: Path) -> tuple[int, int]:
        output = self._git(worktree, "status", "--porcelain=v1", "--untracked-files=all")
        if not output:
            return 0, 0
        lines = output.splitlines()
        deleted = sum(1 for line in lines if "D" in line[:2])
        return len(lines), deleted

    def _run_validation(self, active: ActiveWorktree) -> str | None:
        diff_check = self._run(active.path, "git", "diff", "--check")
        if diff_check.returncode != 0:
            return self._format_failure("validation", diff_check, ("git", "diff", "--check"))
        for command in active.project.validation_commands:
            result = self._run_sandboxed(active, command)
            if result.returncode != 0:
                return self._format_failure("validation", result, command)
        return None

    def _run_sandboxed(
        self,
        active: ActiveWorktree,
        command: tuple[str, ...],
    ) -> subprocess.CompletedProcess[str]:
        sandbox_exec = shutil.which("sandbox-exec")
        if sandbox_exec is None:
            return subprocess.CompletedProcess(
                list(command),
                126,
                "",
                "sandbox-exec is required for configured validation commands",
            )

        validation_root = self.runtime_root / "_validation" / active.task_id
        isolated_home = validation_root / "home"
        isolated_tmp = validation_root / "tmp"
        isolated_home.mkdir(parents=True, exist_ok=True)
        isolated_tmp.mkdir(parents=True, exist_ok=True)

        def sbpl(path: Path) -> str:
            return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')

        real_home = Path.home().resolve()
        profile = "\n".join(
            (
                "(version 1)",
                "(allow default)",
                "(deny network*)",
                "(deny file-write*)",
                '(allow file-write* (literal "/dev/null"))',
                f'(allow file-write* (subpath "{sbpl(active.path)}"))',
                f'(allow file-write* (subpath "{sbpl(validation_root)}"))',
                f'(deny file-read* (subpath "{sbpl(real_home / ".ssh")}"))',
                f'(deny file-read* (subpath "{sbpl(real_home / ".aws")}"))',
                f'(deny file-read* (subpath "{sbpl(real_home / ".codex")}"))',
                f'(deny file-read* (subpath "{sbpl(real_home / ".hermes")}"))',
                f'(deny file-read* (subpath "{sbpl(real_home / "Library/Keychains")}"))',
            )
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
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
        }
        environment.update(
            {
                "HOME": str(isolated_home),
                "TMPDIR": str(isolated_tmp),
                "XDG_CACHE_HOME": str(isolated_home / ".cache"),
                "XDG_CONFIG_HOME": str(isolated_home / ".config"),
            }
        )
        try:
            return self._run(
                active.path,
                sandbox_exec,
                "-p",
                profile,
                *command,
                env=environment,
            )
        finally:
            shutil.rmtree(validation_root, ignore_errors=True)

    def _check_merge_preconditions(self, active: ActiveWorktree) -> str | None:
        repo = active.project.path.resolve()
        current_branch = self._git(repo, "branch", "--show-current")
        if current_branch != active.base_branch:
            return f"base checkout moved to branch {current_branch!r}"
        if self._git(repo, "status", "--porcelain"):
            return "base checkout is no longer clean"
        current_commit = self._git(repo, "rev-parse", active.base_branch)
        if current_commit != active.base_commit:
            return "base branch changed while the task was running"
        return None

    def _commit(self, active: ActiveWorktree, message: str) -> str:
        self._git(active.path, "add", "--all")
        result = self._run(active.path, "git", "commit", "-m", message)
        if result.returncode != 0:
            raise RuntimeError(self._format_failure("commit", result))
        return self._git(active.path, "rev-parse", "HEAD")

    def _cleanup(self, active: ActiveWorktree) -> None:
        repo = active.project.path.resolve()
        result = self._run(repo, "git", "worktree", "remove", str(active.path))
        if result.returncode != 0:
            raise RuntimeError(self._format_failure("worktree cleanup", result))
        result = self._run(repo, "git", "branch", "-d", active.branch_name)
        if result.returncode != 0:
            raise RuntimeError(self._format_failure("task branch cleanup", result))

    def _git(self, cwd: Path, *args: str) -> str:
        result = self._run(cwd, "git", *args)
        if result.returncode != 0:
            raise RuntimeError(self._format_failure("git", result, ("git", *args)))
        return result.stdout.strip()

    @staticmethod
    def _run(
        cwd: Path,
        *argv: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    @staticmethod
    def _format_failure(
        label: str,
        result: subprocess.CompletedProcess[str],
        command: tuple[str, ...] | None = None,
    ) -> str:
        detail = (result.stderr or result.stdout).strip()
        prefix = f"{label} failed"
        if command:
            prefix += f" ({' '.join(command)})"
        return f"{prefix}: {detail}" if detail else prefix
