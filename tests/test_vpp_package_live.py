from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from hermes_wecom_code_governor.config import load_governor_config
from hermes_wecom_code_governor.project_job import ProjectJobRunner

ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.mark.skipif(
    os.environ.get("RUN_VPP_PACKAGE_LIVE") != "1",
    reason="set RUN_VPP_PACKAGE_LIVE=1 to build the real VPP Windows installer",
)
def test_real_vpp_windows_package_is_built_in_an_ephemeral_worktree() -> None:
    config = load_governor_config(ROOT / "config" / "governor.local.yaml")
    project = config.policy.project("vpp-digital-twin")
    original_commit = git(project.path, "rev-parse", project.base_branch or "main")
    original_status = git(project.path, "status", "--porcelain")
    original_worktrees = git(project.path, "worktree", "list", "--porcelain")
    assert original_status == ""
    live_root = ROOT / ".runtime" / "live-vpp-tests"
    live_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=live_root) as directory:
        runner = ProjectJobRunner(
            Path(directory),
            codex_binary=config.codex.codex_bin,
        )
        result = runner.run(
            project,
            job_id="0818-vpp-windows-package",
            argv=(
                "npm",
                "run",
                "build:win",
                "--",
                "--config.electronDownload.isVerifyChecksum=false",
            ),
            artifact_globs=("release/*.exe",),
        )

        assert result.status == "completed", (
            f"exit_code={result.exit_code}\noutput={result.output!r}"
        )
        assert len(result.artifacts) == 1
        assert result.artifacts[0].suffix == ".exe"
        assert result.artifacts[0].stat().st_size > 100 * 1024 * 1024
        assert git(project.path, "rev-parse", project.base_branch or "main") == original_commit
        assert git(project.path, "status", "--porcelain") == original_status
        assert git(project.path, "worktree", "list", "--porcelain") == original_worktrees
