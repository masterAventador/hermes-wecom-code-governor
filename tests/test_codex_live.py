from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_wecom_code_governor.codex_runtime import (
    CodexAppServerRunner,
    CodexMode,
    CodexRunRequest,
    CodexRuntimeSettings,
    CodexTaskState,
)
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


@pytest.mark.skipif(
    os.environ.get("RUN_CODEX_LIVE") != "1",
    reason="set RUN_CODEX_LIVE=1 to use the local ChatGPT Codex subscription",
)
def test_live_codex_app_server_edits_only_the_worktree_then_governor_merges(
    tmp_path: Path,
) -> None:
    codex_bin = shutil.which("codex")
    assert codex_bin is not None
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test Bot")
    git(repo, "config", "user.email", "bot@example.test")
    (repo / "README.md").write_text("live integration\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    git(repo, "switch", "-c", "dev")

    outside = tmp_path / "outside.txt"
    outside.write_text("must stay unchanged\n", encoding="utf-8")
    manager = WorktreeManager(tmp_path / "runtime")
    project = Project("live", "Live", repo)
    active = manager.begin(project, "live-codex-sdk")
    readable, writable = manager.codex_roots(active)
    runner = CodexAppServerRunner(
        CodexRuntimeSettings(
            model="gpt-5.6-sol",
            reasoning_effort="xhigh",
            codex_bin=Path(codex_bin),
        )
    )

    result = runner.run(
        CodexRunRequest(
            mode=CodexMode.WRITE,
            prompt=(
                "在当前仓库新建 result.txt，内容必须恰好是 native-codex-ok 加一个换行。"
                "不要修改其他文件。完成后检查文件内容。"
            ),
            cwd=active.path,
            readable_roots=readable,
            writable_roots=writable,
        )
    )

    assert result.task_state is CodexTaskState.COMPLETED
    assert (active.path / "result.txt").read_text(encoding="utf-8") == "native-codex-ok\n"
    assert outside.read_text(encoding="utf-8") == "must stay unchanged\n"
    resumed = runner.run(
        CodexRunRequest(
            mode=CodexMode.WRITE,
            prompt=(
                "继续刚才的任务，复查 result.txt 是否满足上一轮要求。"
                "如果已经正确，不要再修改文件，直接报告完成。"
            ),
            cwd=active.path,
            thread_id=result.thread_id,
            readable_roots=readable,
            writable_roots=writable,
        )
    )
    assert resumed.thread_id == result.thread_id
    assert resumed.task_state is CodexTaskState.COMPLETED
    completion = manager.complete(
        active,
        SafetyLimits(max_changed_files=2, max_deleted_files=0),
    )
    assert completion.status is CompletionStatus.MERGED
    assert (repo / "result.txt").read_text(encoding="utf-8") == "native-codex-ok\n"
    assert not active.path.exists()
