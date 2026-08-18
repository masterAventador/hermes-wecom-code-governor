from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_wecom_code_governor.policy import Project
from hermes_wecom_code_governor.project_job import (
    CodexSandboxExecutor,
    JobExecutionRequest,
    JobExecutionResult,
    ProjectJobRunner,
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test Bot")
    git(repo, "config", "user.email", "bot@example.test")
    (repo / "README.md").write_text("original\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    return repo


def project(repo: Path, **changes: object) -> Project:
    values: dict[str, object] = {
        "project_id": "demo",
        "display_name": "Demo",
        "path": repo,
        "base_branch": "main",
        "job_allowed_commands": (("./build-artifact",),),
        "job_artifact_globs": ("release/*.bin",),
        "job_timeout_seconds": 120,
    }
    values.update(changes)
    return Project(**values)  # type: ignore[arg-type]


@dataclass
class FakeExecutor:
    result: JobExecutionResult = JobExecutionResult(0, "built", "")

    def __post_init__(self) -> None:
        self.requests: list[JobExecutionRequest] = []

    def run(self, request: JobExecutionRequest) -> JobExecutionResult:
        self.requests.append(request)
        assert git(request.cwd, "branch", "--show-current") == ""
        assert (request.cwd / "README.md").read_text(encoding="utf-8") == "original\n"
        (request.cwd / "README.md").unlink()
        release = request.cwd / "release"
        release.mkdir()
        (release / "demo.bin").write_bytes(b"artifact")
        return self.result


def test_job_runs_in_detached_worktree_stages_artifact_and_removes_workspace(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    executor = FakeExecutor()
    runtime_root = tmp_path / "runtime"
    runner = ProjectJobRunner(runtime_root, executor=executor)
    original_commit = git(repo, "rev-parse", "main")

    result = runner.run(
        project(repo),
        job_id="0818-build-package--message1",
        argv=("./build-artifact",),
        artifact_globs=("release/*.bin",),
    )

    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.output == "built"
    assert result.base_commit == original_commit
    assert len(result.artifacts) == 1
    assert result.artifacts[0].read_bytes() == b"artifact"
    assert result.artifacts[0].is_relative_to(runtime_root / "artifacts")
    assert not (runtime_root / "jobs" / "demo" / "0818-build-package--message1").exists()
    assert (repo / "README.md").read_text(encoding="utf-8") == "original\n"
    assert git(repo, "rev-parse", "main") == original_commit
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.mark.parametrize(
    "argv",
    (
        ("npm", "run", "unknown"),
        ("sh", "-c", "rm -rf ."),
        ("./build-artifact", "--unexpected"),
    ),
)
def test_job_rejects_commands_outside_exact_admin_patterns(
    tmp_path: Path,
    argv: tuple[str, ...],
) -> None:
    repo = create_repo(tmp_path)
    runner = ProjectJobRunner(tmp_path / "runtime", executor=FakeExecutor())

    with pytest.raises(PermissionError, match="command is not allowed"):
        runner.run(project(repo), job_id="0818-denied", argv=argv)

    assert not (tmp_path / "runtime" / "jobs").exists()


def test_one_token_wildcard_does_not_allow_extra_arguments(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    executor = FakeExecutor()
    runner = ProjectJobRunner(tmp_path / "runtime", executor=executor)
    configured = project(
        repo,
        job_allowed_commands=(("npm", "run", "*"),),
        job_artifact_globs=(),
    )

    runner.run(configured, job_id="0818-test", argv=("npm", "run", "test"))
    with pytest.raises(PermissionError, match="command is not allowed"):
        runner.run(
            configured,
            job_id="0818-injected",
            argv=("npm", "run", "test", "--", "--runInBand"),
        )


def test_job_rejects_unapproved_artifact_globs_before_execution(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    runner = ProjectJobRunner(tmp_path / "runtime", executor=FakeExecutor())

    with pytest.raises(PermissionError, match="artifact glob is not allowed"):
        runner.run(
            project(repo),
            job_id="0818-exfiltrate",
            argv=("./build-artifact",),
            artifact_globs=("../*.txt",),
        )


def test_failed_job_cleans_worktree_and_does_not_stage_artifacts(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    executor = FakeExecutor(JobExecutionResult(3, "", "build failed"))
    runtime_root = tmp_path / "runtime"
    runner = ProjectJobRunner(runtime_root, executor=executor)

    result = runner.run(
        project(repo),
        job_id="0818-failed",
        argv=("./build-artifact",),
        artifact_globs=("release/*.bin",),
    )

    assert result.status == "failed"
    assert result.exit_code == 3
    assert result.output == "build failed"
    assert result.artifacts == ()
    assert not (runtime_root / "jobs" / "demo" / "0818-failed").exists()
    assert (repo / "README.md").exists()


def test_executor_uses_minimal_read_access_exact_write_roots_and_sanitized_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "workspace"
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    for path in (worktree, home, temporary):
        path.mkdir()
    request = JobExecutionRequest(
        cwd=worktree,
        home=home,
        temporary=temporary,
        argv=("npm", "test"),
        timeout_seconds=60,
        readable_paths=(Path("/opt/homebrew"),),
        unix_sockets=(Path("/private/var/run/mDNSResponder"),),
    )
    monkeypatch.setenv("WECOM_SECRET", "must-not-leak")
    monkeypatch.setenv("COS_SECRET_KEY", "must-not-leak")
    executor = CodexSandboxExecutor(Path("/opt/homebrew/bin/codex"))

    environment = executor.build_environment(request, os.environ)
    state = executor.build_sandbox_state(request)
    command = executor.build_command(request)

    assert "WECOM_SECRET" not in environment
    assert "COS_SECRET_KEY" not in environment
    assert environment["HOME"] == str(home)
    assert environment["TMPDIR"] == str(temporary)
    assert state["sandboxPolicy"] == {"type": "read-only"}
    entries = state["permissionProfile"]["file_system"]["entries"]
    assert entries[0] == {
        "path": {"type": "special", "value": {"kind": "minimal"}},
        "access": "read",
    }
    assert {
        "path": {"type": "path", "path": str(worktree.resolve())},
        "access": "write",
    } in entries
    assert {
        "path": {"type": "path", "path": str(home.resolve())},
        "access": "write",
    } in entries
    assert {
        "path": {"type": "path", "path": "/opt/homebrew"},
        "access": "read",
    } in entries
    assert state["permissionProfile"]["network"] == "restricted"
    assert command[0] == str(Path("/opt/homebrew/bin/codex").resolve())
    assert command[1:3] == ("sandbox", "--log-denials")
    assert command[3:5] == (
        "--allow-unix-socket",
        "/private/var/run/mDNSResponder",
    )
    assert command[-3:] == ("--", "npm", "test")


def test_project_dependencies_and_build_caches_are_copied_into_the_isolated_job(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore dependencies")
    dependency = repo / "node_modules" / "demo-package" / "index.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("original dependency\n", encoding="utf-8")
    cache_source = tmp_path / "electron-cache"
    cache_source.mkdir()
    (cache_source / "electron.zip").write_bytes(b"cached runtime")
    external_cache_target = tmp_path / "external-cache-target"
    external_cache_target.mkdir()
    (external_cache_target / "outside.txt").write_text("outside\n", encoding="utf-8")
    (cache_source / "root-link").symlink_to(external_cache_target, target_is_directory=True)

    class SeedAwareExecutor:
        def run(self, request: JobExecutionRequest) -> JobExecutionResult:
            isolated_dependency = request.cwd / "node_modules" / "demo-package" / "index.js"
            isolated_cache = request.home / "Library" / "Caches" / "electron" / "electron.zip"
            isolated_link = request.home / "Library" / "Caches" / "electron" / "root-link"
            assert isolated_dependency.read_text(encoding="utf-8") == "original dependency\n"
            assert isolated_cache.read_bytes() == b"cached runtime"
            assert isolated_link.is_symlink()
            assert os.readlink(isolated_link) == str(external_cache_target)
            isolated_dependency.write_text("job-only change\n", encoding="utf-8")
            isolated_cache.write_bytes(b"job-only cache change")
            return JobExecutionResult(0, "ok", "")

    runner = ProjectJobRunner(tmp_path / "runtime", executor=SeedAwareExecutor())
    configured = project(
        repo,
        seed_paths=("node_modules",),
        job_home_seeds=((cache_source, Path("Library/Caches/electron")),),
        job_artifact_globs=(),
    )

    result = runner.run(configured, job_id="0818-seeds", argv=("./build-artifact",))

    assert result.status == "completed"
    assert dependency.read_text(encoding="utf-8") == "original dependency\n"
    assert (cache_source / "electron.zip").read_bytes() == b"cached runtime"


def test_project_seed_path_cannot_escape_the_repository(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    runner = ProjectJobRunner(tmp_path / "runtime", executor=FakeExecutor())

    with pytest.raises(PermissionError, match="seed path"):
        runner.run(
            project(repo, seed_paths=("../outside",)),
            job_id="0818-seed-escape",
            argv=("./build-artifact",),
        )


def test_runner_requires_a_real_codex_executable_when_no_executor_is_injected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hermes_wecom_code_governor.project_job.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="codex executable"):
        ProjectJobRunner(tmp_path / "runtime")


def test_job_output_keeps_the_useful_tail_without_flooding_the_outer_agent(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    executor = FakeExecutor(JobExecutionResult(1, "x" * 20_000, "final failure"))
    runner = ProjectJobRunner(tmp_path / "runtime", executor=executor)

    result = runner.run(
        project(repo, job_artifact_globs=()),
        job_id="0818-capped-output",
        argv=("./build-artifact",),
    )

    assert len(result.output) <= 12_100
    assert result.output.startswith("[前面 ")
    assert result.output.endswith("final failure")
