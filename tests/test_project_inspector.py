from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from hermes_wecom_code_governor.policy import Project
from hermes_wecom_code_governor.project_inspector import ProjectInspector


def project(path: Path) -> Project:
    return Project("demo", "演示项目", path)


def test_files_can_return_latest_file_metadata_and_sha256(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    older = release / "older.exe"
    latest = release / "latest.exe"
    older.write_bytes(b"older")
    latest.write_bytes(b"latest")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(latest, (1_800_000_000, 1_800_000_000))

    result = ProjectInspector().files(
        project(tmp_path),
        path="release",
        pattern="*.exe",
        sort="mtime_desc",
        limit=1,
        sha256=True,
    )

    assert result["files"] == [
        {
            "path": "release/latest.exe",
            "size_bytes": 6,
            "modified_at": "2027-01-15T16:00:00+08:00",
            "sha256": hashlib.sha256(b"latest").hexdigest(),
        }
    ]


def test_read_supports_multiple_text_files_with_line_bounds(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("一\n二\n三\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")

    result = ProjectInspector().read(
        project(tmp_path),
        paths=["README.md", "package.json"],
        start_line=2,
        max_lines=2,
    )

    assert result["files"][0] == {
        "path": "README.md",
        "start_line": 2,
        "end_line": 3,
        "truncated": False,
        "content": "二\n三",
    }
    assert result["files"][1]["content"] == ""


def test_search_is_literal_bounded_and_skips_dependencies_and_secrets(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("Alpha\nneedle here\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("needle\n", encoding="utf-8")
    (tmp_path / ".env").write_text("needle=secret\n", encoding="utf-8")

    result = ProjectInspector().search(
        project(tmp_path),
        query="needle",
        path=".",
        patterns=["*.py", "*.env"],
        limit=10,
    )

    assert result == {
        "matches": [{"path": "src/main.py", "line": 2, "text": "needle here"}],
        "truncated": False,
    }


@pytest.mark.parametrize("path", ["../outside.txt", ".env", ".git/config"])
def test_direct_read_rejects_paths_outside_the_safe_project_surface(
    tmp_path: Path, path: str
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret", encoding="utf-8")
    (tmp_path / ".env").write_text("secret", encoding="utf-8")

    with pytest.raises(PermissionError):
        ProjectInspector().read(project(tmp_path), paths=[path])


def test_symlink_to_file_outside_project_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-project.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "escape.txt").symlink_to(outside)

    with pytest.raises(PermissionError):
        ProjectInspector().read(project(tmp_path), paths=["escape.txt"])


def test_git_exposes_only_fixed_read_only_actions(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True
    )
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)

    inspector = ProjectInspector()

    assert "initial" in inspector.git(project(tmp_path), action="log", limit=1)["output"]
    with pytest.raises(ValueError, match="action"):
        inspector.git(project(tmp_path), action="reset")
