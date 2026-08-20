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


@pytest.mark.skipif(
    os.environ.get("RUN_VPP_MAC_LIVE") != "1",
    reason="set RUN_VPP_MAC_LIVE=1 to build, sign and notarize the real VPP mac dmg",
)
def test_real_vpp_mac_dmg_is_built_signed_and_notarized_on_the_trusted_tier() -> None:
    config = load_governor_config(ROOT / "config" / "governor.local.yaml")
    project = config.policy.project("vpp-digital-twin")
    original_commit = git(project.path, "rev-parse", project.base_branch or "main")
    original_status = git(project.path, "status", "--porcelain")
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
            job_id="0820-vpp-mac-package",
            argv=("npm", "run", "build:mac"),
            artifact_globs=("release/*.dmg",),
        )

        assert result.status == "completed", (
            f"exit_code={result.exit_code}\noutput={result.output!r}"
        )
        assert len(result.artifacts) == 1
        dmg = result.artifacts[0]
        assert dmg.suffix == ".dmg"
        assert dmg.stat().st_size > 100 * 1024 * 1024

        # 验收落在用户真实路径：挂载 DMG → 打开里面的 .app → Gatekeeper 放行。
        # electron-builder 公证并装订的是 DMG 内的 .app，DMG 容器本身不单独签名，
        # 所以断言对象是 app，不是 dmg。
        attach = subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-readonly", str(dmg)],
            capture_output=True,
            text=True,
            check=True,
        )
        mount_point = next(
            line.split("\t")[-1].strip()
            for line in attach.stdout.splitlines()
            if "/Volumes/" in line
        )
        try:
            app = next(Path(mount_point).glob("*.app"))
            spctl = subprocess.run(
                ["spctl", "-a", "-vvv", str(app)],
                capture_output=True,
                text=True,
            )
            assert spctl.returncode == 0, spctl.stderr
            assert "Notarized Developer ID" in spctl.stderr
            staple = subprocess.run(
                ["xcrun", "stapler", "validate", str(app)],
                capture_output=True,
                text=True,
            )
            assert staple.returncode == 0, staple.stdout + staple.stderr
        finally:
            subprocess.run(["hdiutil", "detach", mount_point], capture_output=True, check=False)

        assert git(project.path, "rev-parse", project.base_branch or "main") == original_commit
        assert git(project.path, "status", "--porcelain") == ""
