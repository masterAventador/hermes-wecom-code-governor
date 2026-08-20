from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_wecom_code_governor.policy import Project
from hermes_wecom_code_governor.worktree import (
    CompletionStatus,
    SafetyLimits,
    WorktreeManager,
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_repo(tmp_path: Path, *, with_dev: bool = True) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test Bot")
    git(repo, "config", "user.email", "bot@example.test")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    if with_dev:
        git(repo, "switch", "-c", "dev")
    return repo


def create_repo_with_separate_git_dir(tmp_path: Path) -> Path:
    repo = tmp_path / "separate-project"
    git_dir = tmp_path / "separate-project.git"
    subprocess.run(
        ["git", "init", "--separate-git-dir", str(git_dir), "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    git(repo, "config", "user.name", "Test Bot")
    git(repo, "config", "user.email", "bot@example.test")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    git(repo, "switch", "-c", "dev")
    return repo


def project(repo: Path, **changes: object) -> Project:
    values: dict[str, object] = {
        "project_id": "demo",
        "display_name": "Demo",
        "path": repo,
        "validation_commands": (("git", "diff", "--check"),),
    }
    values.update(changes)
    return Project(**values)  # type: ignore[arg-type]


def test_begin_uses_dev_and_creates_isolated_worktree(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    manager = WorktreeManager(tmp_path / "runtime")

    active = manager.begin(project(repo), "0817-update-readme")

    assert active.base_branch == "dev"
    assert active.branch_name == "bot/0817-update-readme"
    assert active.path.exists()
    assert git(active.path, "branch", "--show-current") == active.branch_name
    assert git(active.path, "rev-parse", "HEAD") == git(repo, "rev-parse", "dev")

    readable, writable = manager.codex_roots(active)
    git_dir = Path(git(active.path, "rev-parse", "--git-dir")).resolve()
    assert readable == (active.path.resolve(), (repo / ".git").resolve())
    assert writable == (active.path.resolve(), git_dir)


def test_codex_roots_support_a_repository_whose_dot_git_is_a_pointer_file(
    tmp_path: Path,
) -> None:
    repo = create_repo_with_separate_git_dir(tmp_path)
    manager = WorktreeManager(tmp_path / "runtime")

    active = manager.begin(project(repo), "0817-separate-git-dir")
    readable, writable = manager.codex_roots(active)

    common_git_dir = Path(git(repo, "rev-parse", "--git-common-dir")).resolve()
    worktree_git_dir = Path(git(active.path, "rev-parse", "--absolute-git-dir")).resolve()
    assert readable == (active.path.resolve(), common_git_dir)
    assert writable == (active.path.resolve(), worktree_git_dir)


def test_begin_accepts_short_semantic_chinese_task_name(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    manager = WorktreeManager(tmp_path / "runtime")

    active = manager.begin(project(repo), "0817-修改说明标题")

    assert active.branch_name == "bot/0817-修改说明标题"
    assert git(active.path, "branch", "--show-current") == active.branch_name


def create_repo_with_ignored_dependencies(tmp_path: Path) -> Path:
    repo = create_repo(tmp_path)
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore dependencies")
    dependency = repo / "node_modules" / "dep"
    dependency.mkdir(parents=True)
    (dependency / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    return repo


def test_begin_seeds_configured_dependency_paths_into_the_worktree(tmp_path: Path) -> None:
    repo = create_repo_with_ignored_dependencies(tmp_path)
    manager = WorktreeManager(tmp_path / "runtime")

    active = manager.begin(project(repo, seed_paths=("node_modules",)), "0818-seeded")

    assert (active.path / "node_modules" / "dep" / "index.js").is_file()
    result = manager.complete(active, SafetyLimits(max_changed_files=10, max_deleted_files=2))
    assert result.status is CompletionStatus.NO_CHANGES
    assert not active.path.exists()


def test_ensure_seeded_backfills_an_existing_worktree_and_is_idempotent(
    tmp_path: Path,
) -> None:
    repo = create_repo_with_ignored_dependencies(tmp_path)
    manager = WorktreeManager(tmp_path / "runtime")
    configured = project(repo, seed_paths=("node_modules",))
    active = manager.begin(configured, "0818-backfill")
    shutil.rmtree(active.path / "node_modules")

    manager.ensure_seeded(active)
    assert (active.path / "node_modules" / "dep" / "index.js").is_file()

    manager.ensure_seeded(active)
    assert (active.path / "node_modules" / "dep" / "index.js").is_file()


def test_begin_rejects_a_seed_path_escaping_the_repository(tmp_path: Path) -> None:
    repo = create_repo_with_ignored_dependencies(tmp_path)
    manager = WorktreeManager(tmp_path / "runtime")

    with pytest.raises(PermissionError, match="seed path"):
        manager.begin(project(repo, seed_paths=("../outside",)), "0818-seed-escape")


def test_begin_falls_back_to_main_without_creating_dev(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, with_dev=False)
    manager = WorktreeManager(tmp_path / "runtime")

    active = manager.begin(project(repo), "0817-main-task")

    assert active.base_branch == "main"
    assert "dev" not in git(repo, "branch", "--format=%(refname:short)").splitlines()


def test_configured_missing_base_branch_fails_without_creating_worktree(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, with_dev=False)
    manager = WorktreeManager(tmp_path / "runtime")

    with pytest.raises(RuntimeError, match="base branch"):
        manager.begin(project(repo, base_branch="release"), "0817-task")

    assert not (tmp_path / "runtime").exists()


def test_begin_requires_clean_base_checkout_on_the_base_branch(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    manager = WorktreeManager(tmp_path / "runtime")

    with pytest.raises(RuntimeError, match="clean"):
        manager.begin(project(repo), "0817-task")


def create_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    repo = create_repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "dev")
    return repo, remote


def test_push_on_merge_pushes_base_branch_to_remote(tmp_path: Path) -> None:
    repo, remote = create_repo_with_remote(tmp_path)
    manager = WorktreeManager(tmp_path / "runtime")
    active = manager.begin(project(repo, push_on_merge=True), "0820-push")
    (active.path / "README.md").write_text("changed\n", encoding="utf-8")

    result = manager.complete(active, SafetyLimits(max_changed_files=10, max_deleted_files=2))

    assert result.status is CompletionStatus.MERGED
    assert result.message == ""
    remote_head = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "dev"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert remote_head == git(repo, "rev-parse", "dev")


def test_push_on_merge_failure_still_reports_local_merge(tmp_path: Path) -> None:
    repo, remote = create_repo_with_remote(tmp_path)
    shutil.rmtree(remote)  # 远端消失，push 必失败
    manager = WorktreeManager(tmp_path / "runtime")
    active = manager.begin(project(repo, push_on_merge=True), "0820-push-fail")
    (active.path / "README.md").write_text("changed\n", encoding="utf-8")

    result = manager.complete(active, SafetyLimits(max_changed_files=10, max_deleted_files=2))

    assert result.status is CompletionStatus.MERGED
    assert "push" in result.message.lower()
    # 本地合并已完成：dev 上有机器人这次提交，worktree 和任务分支照常清理。
    assert git(repo, "log", "-1", "--format=%s", "dev") == "机器人：0820-push-fail"
    assert not active.path.exists()


def test_no_push_when_flag_disabled(tmp_path: Path) -> None:
    repo, remote = create_repo_with_remote(tmp_path)
    manager = WorktreeManager(tmp_path / "runtime")
    active = manager.begin(project(repo), "0820-nopush")
    (active.path / "README.md").write_text("changed\n", encoding="utf-8")

    result = manager.complete(active, SafetyLimits(max_changed_files=10, max_deleted_files=2))

    assert result.status is CompletionStatus.MERGED
    remote_head = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "dev"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # 远端仍停在初始提交，本地已领先
    assert remote_head != git(repo, "rev-parse", "dev")


def test_successful_validation_fast_forwards_base_and_cleans_worktree(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    manager = WorktreeManager(tmp_path / "runtime")
    active = manager.begin(project(repo), "0817-update-readme")
    (active.path / "README.md").write_text("changed\n", encoding="utf-8")

    result = manager.complete(active, SafetyLimits(max_changed_files=10, max_deleted_files=2))

    assert result.status is CompletionStatus.MERGED
    assert (repo / "README.md").read_text(encoding="utf-8") == "changed\n"
    assert git(repo, "branch", "--show-current") == "dev"
    assert git(repo, "status", "--porcelain") == ""
    assert not active.path.exists()
    assert active.branch_name not in git(repo, "branch", "--format=%(refname:short)").splitlines()
    assert result.commit == git(repo, "rev-parse", "dev")[:7]


def test_failed_validation_keeps_worktree_and_does_not_change_base(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    original = git(repo, "rev-parse", "dev")
    manager = WorktreeManager(tmp_path / "runtime")
    active = manager.begin(
        project(repo, validation_commands=(("git", "diff", "--exit-code"),)),
        "0817-failing-task",
    )
    (active.path / "README.md").write_text("changed\n", encoding="utf-8")

    result = manager.complete(active, SafetyLimits(max_changed_files=10, max_deleted_files=2))

    assert result.status is CompletionStatus.VALIDATION_FAILED
    assert git(repo, "rev-parse", "dev") == original
    assert active.path.exists()
    assert active.branch_name in git(repo, "branch", "--format=%(refname:short)").splitlines()


_LOOPBACK_SERVER_SCRIPT = (
    "import socket\n"
    "server = socket.socket()\n"
    "server.bind(('0.0.0.0', 0))\n"
    "server.listen(1)\n"
    "port = server.getsockname()[1]\n"
    "client = socket.socket()\n"
    "client.settimeout(5)\n"
    "client.connect(('127.0.0.1', port))\n"
    "server.accept()\n"
)

_EXTERNAL_CONNECT_SCRIPT = (
    "import errno, socket, sys\n"
    "client = socket.socket()\n"
    "client.settimeout(3)\n"
    "try:\n"
    "    client.connect(('192.0.2.1', 9))\n"
    "except OSError as error:\n"
    "    sys.exit(0 if error.errno in (errno.EPERM, errno.EACCES) else 2)\n"
    "sys.exit(2)\n"
)


def test_validation_command_can_use_loopback_network_for_local_test_servers(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    manager = WorktreeManager(tmp_path / "runtime")
    active = manager.begin(
        project(repo, validation_commands=(("python3", "-c", _LOOPBACK_SERVER_SCRIPT),)),
        "0818-loopback",
    )
    (active.path / "README.md").write_text("changed\n", encoding="utf-8")

    result = manager.complete(active, SafetyLimits(max_changed_files=10, max_deleted_files=2))

    assert result.status is CompletionStatus.MERGED


def test_validation_command_still_cannot_reach_external_network(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    manager = WorktreeManager(tmp_path / "runtime")
    active = manager.begin(
        project(repo, validation_commands=(("python3", "-c", _EXTERNAL_CONNECT_SCRIPT),)),
        "0818-no-external",
    )
    (active.path / "README.md").write_text("changed\n", encoding="utf-8")

    result = manager.complete(active, SafetyLimits(max_changed_files=10, max_deleted_files=2))

    assert result.status is CompletionStatus.MERGED


def test_mass_deletion_is_blocked_before_validation_and_keeps_recovery_state(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    for index in range(4):
        (repo / f"file-{index}.txt").write_text(str(index), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add files")
    original = git(repo, "rev-parse", "dev")
    manager = WorktreeManager(tmp_path / "runtime")
    active = manager.begin(project(repo), "0817-delete-files")
    for index in range(4):
        (active.path / f"file-{index}.txt").unlink()

    result = manager.complete(active, SafetyLimits(max_changed_files=10, max_deleted_files=2))

    assert result.status is CompletionStatus.SAFETY_BLOCKED
    assert "deleted files" in result.message
    assert git(repo, "rev-parse", "dev") == original
    assert active.path.exists()


def test_no_changes_cleans_temporary_worktree_without_new_commit(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    original = git(repo, "rev-parse", "dev")
    manager = WorktreeManager(tmp_path / "runtime")
    active = manager.begin(project(repo), "0817-no-change")

    result = manager.complete(active, SafetyLimits(max_changed_files=10, max_deleted_files=2))

    assert result.status is CompletionStatus.NO_CHANGES
    assert git(repo, "rev-parse", "dev") == original
    assert not active.path.exists()


@pytest.mark.skipif(shutil.which("sandbox-exec") is None, reason="requires macOS sandbox-exec")
def test_validation_command_cannot_write_outside_worktree(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    escaped = tmp_path / "escaped.txt"
    manager = WorktreeManager(tmp_path / "runtime")
    active = manager.begin(
        project(
            repo,
            validation_commands=(("sh", "-c", f"printf compromised > {escaped}"),),
        ),
        "0817-sandbox",
    )
    (active.path / "README.md").write_text("changed\n", encoding="utf-8")

    result = manager.complete(active, SafetyLimits(max_changed_files=10, max_deleted_files=2))

    assert result.status is CompletionStatus.VALIDATION_FAILED
    assert not escaped.exists()
    assert active.path.exists()


@pytest.mark.skipif(shutil.which("sandbox-exec") is None, reason="requires macOS sandbox-exec")
def test_validation_command_does_not_receive_bot_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = create_repo(tmp_path)
    monkeypatch.setenv("WECOM_SECRET", "must-not-leak")
    monkeypatch.setenv("COS_SECRET_KEY", "must-not-leak")
    manager = WorktreeManager(tmp_path / "runtime")
    active = manager.begin(
        project(
            repo,
            validation_commands=(
                ("sh", "-c", 'test -z "$WECOM_SECRET" && test -z "$COS_SECRET_KEY"'),
            ),
        ),
        "0817-secret-env",
    )
    (active.path / "README.md").write_text("changed\n", encoding="utf-8")

    result = manager.complete(active, SafetyLimits(max_changed_files=10, max_deleted_files=2))

    assert result.status is CompletionStatus.MERGED


@pytest.mark.skipif(shutil.which("sandbox-exec") is None, reason="requires macOS sandbox-exec")
def test_validation_generated_files_are_rechecked_against_safety_limits(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    original = git(repo, "rev-parse", "dev")
    manager = WorktreeManager(tmp_path / "runtime")
    active = manager.begin(
        project(
            repo,
            validation_commands=(("sh", "-c", "touch generated-1 generated-2 generated-3"),),
        ),
        "0817-generated-files",
    )
    (active.path / "README.md").write_text("changed\n", encoding="utf-8")

    result = manager.complete(active, SafetyLimits(max_changed_files=2, max_deleted_files=2))

    assert result.status is CompletionStatus.SAFETY_BLOCKED
    assert "changed files" in result.message
    assert git(repo, "rev-parse", "dev") == original
    assert active.path.exists()
