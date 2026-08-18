from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from hermes_wecom_code_governor.policy import Project
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
    os.environ.get("RUN_LIVE_JOB_SANDBOX") != "1",
    reason="set RUN_LIVE_JOB_SANDBOX=1 to run the macOS Codex sandbox acceptance",
)
def test_real_job_sandbox_blocks_host_access_network_and_secret_environment() -> None:
    live_root = ROOT / ".runtime" / "live-job-tests"
    live_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=live_root) as directory:
        case_root = Path(directory)
        repo = case_root / "project"
        repo.mkdir()
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.name", "Test Bot")
        git(repo, "config", "user.email", "bot@example.test")
        escaped = case_root / "escaped.txt"
        script = repo / "build-artifact"
        script.write_text(
            "\n".join(
                (
                    "#!/bin/sh",
                    "set -eu",
                    'test -z "${WECOM_SECRET:-}"',
                    "if /usr/bin/head -n 1 /Users/aventador/.claude/CLAUDE.md "
                    ">/dev/null 2>&1; then exit 70; fi",
                    f"if /usr/bin/touch {escaped} 2>/dev/null; then exit 71; fi",
                    "if /usr/bin/curl --max-time 2 -I https://example.com "
                    ">/dev/null 2>&1; then exit 72; fi",
                    "mkdir -p release",
                    "printf artifact > release/app.bin",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        (repo / "README.md").write_text("original\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "initial")
        runner = ProjectJobRunner(
            case_root / "runtime",
            codex_binary=Path("/opt/homebrew/bin/codex"),
        )
        project = Project(
            "sandbox-live",
            "Sandbox live",
            repo,
            base_branch="main",
            job_allowed_commands=(("./build-artifact",),),
            job_artifact_globs=("release/*.bin",),
            job_timeout_seconds=30,
        )

        result = runner.run(
            project,
            job_id="0818-live-sandbox",
            argv=("./build-artifact",),
            artifact_globs=("release/*.bin",),
        )

        assert result.status == "completed", result.output
        assert result.artifacts[0].read_bytes() == b"artifact"
        assert not escaped.exists()
        assert (repo / "README.md").read_text(encoding="utf-8") == "original\n"
