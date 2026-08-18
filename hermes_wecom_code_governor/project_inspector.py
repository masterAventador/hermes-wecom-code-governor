from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .policy import Project

_MAX_READ_FILES = 10
_MAX_READ_BYTES = 512 * 1024
_MAX_SEARCH_BYTES = 1024 * 1024
_MAX_RESULTS = 100
_SKIPPED_SEARCH_DIRS = frozenset({".git", ".venv", "venv", "node_modules"})
_REVISION = re.compile(r"[A-Za-z0-9_./~^{}-]{1,100}")


class ProjectInspector:
    """Structured read-only inspection constrained to one authorized project."""

    def files(
        self,
        project: Project,
        *,
        path: str = ".",
        pattern: str = "*",
        recursive: bool = False,
        limit: int = 50,
        sort: str = "path",
        sha256: bool = False,
    ) -> dict[str, Any]:
        root = self._project_root(project)
        base = self._resolve(root, path)
        if not base.is_dir():
            raise ValueError("project files path must be a directory")
        self._validate_pattern(pattern)
        self._validate_limit(limit)
        if sort not in {"path", "mtime_desc"}:
            raise ValueError("project files sort must be path or mtime_desc")

        candidates: list[tuple[Path, os.stat_result]] = []
        iterator = base.rglob(pattern) if recursive else base.glob(pattern)
        for candidate in iterator:
            try:
                resolved = candidate.resolve(strict=True)
                self._assert_safe(root, resolved)
            except (FileNotFoundError, PermissionError, RuntimeError):
                continue
            if resolved.is_file():
                candidates.append((resolved, resolved.stat()))

        if sort == "mtime_desc":
            candidates.sort(key=lambda item: (-item[1].st_mtime_ns, str(item[0])))
        else:
            candidates.sort(key=lambda item: str(item[0].relative_to(root)))

        files = []
        for resolved, stat in candidates[:limit]:
            item: dict[str, Any] = {
                "path": str(resolved.relative_to(root)),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime)
                .astimezone()
                .isoformat(timespec="seconds"),
            }
            if sha256:
                item["sha256"] = self._sha256(resolved)
            files.append(item)
        return {"files": files}

    def read(
        self,
        project: Project,
        *,
        paths: list[str],
        start_line: int = 1,
        max_lines: int = 200,
    ) -> dict[str, Any]:
        if not isinstance(paths, list) or not 1 <= len(paths) <= _MAX_READ_FILES:
            raise ValueError(f"project read paths must contain 1-{_MAX_READ_FILES} files")
        if not isinstance(start_line, int) or isinstance(start_line, bool) or start_line < 1:
            raise ValueError("project read start_line must be a positive integer")
        if (
            not isinstance(max_lines, int)
            or isinstance(max_lines, bool)
            or not 1 <= max_lines <= 1000
        ):
            raise ValueError("project read max_lines must be between 1 and 1000")

        root = self._project_root(project)
        files = []
        for path in paths:
            resolved = self._resolve(root, path)
            if not resolved.is_file():
                raise ValueError(f"project read path is not a file: {path}")
            data = resolved.read_bytes()
            if len(data) > _MAX_READ_BYTES:
                raise ValueError(f"project read file exceeds {_MAX_READ_BYTES} bytes: {path}")
            if b"\0" in data:
                raise ValueError(f"project read file is binary: {path}")
            lines = data.decode("utf-8", errors="replace").splitlines()
            start_index = start_line - 1
            selected = lines[start_index : start_index + max_lines]
            files.append(
                {
                    "path": str(resolved.relative_to(root)),
                    "start_line": start_line,
                    "end_line": start_line + len(selected) - 1,
                    "truncated": start_index + len(selected) < len(lines),
                    "content": "\n".join(selected),
                }
            )
        return {"files": files}

    def search(
        self,
        project: Project,
        *,
        query: str,
        path: str = ".",
        patterns: list[str] | None = None,
        limit: int = 50,
        case_sensitive: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query or len(query) > 500:
            raise ValueError("project search query must contain 1-500 characters")
        self._validate_limit(limit)
        patterns = patterns or ["*"]
        if not isinstance(patterns, list) or not 1 <= len(patterns) <= 20:
            raise ValueError("project search patterns must contain 1-20 globs")
        for pattern in patterns:
            self._validate_pattern(pattern)

        root = self._project_root(project)
        base = self._resolve(root, path)
        if not base.is_dir():
            raise ValueError("project search path must be a directory")
        needle = query if case_sensitive else query.casefold()
        matches: list[dict[str, Any]] = []
        for current, directories, filenames in os.walk(base, followlinks=False):
            directories[:] = sorted(
                directory
                for directory in directories
                if directory not in _SKIPPED_SEARCH_DIRS and not self._denied_name(directory)
            )
            for filename in sorted(filenames):
                if self._denied_name(filename) or not any(
                    fnmatch.fnmatch(filename, pattern) for pattern in patterns
                ):
                    continue
                candidate = Path(current) / filename
                try:
                    resolved = candidate.resolve(strict=True)
                    self._assert_safe(root, resolved)
                except (FileNotFoundError, PermissionError, RuntimeError):
                    continue
                if not resolved.is_file() or resolved.stat().st_size > _MAX_SEARCH_BYTES:
                    continue
                data = resolved.read_bytes()
                if b"\0" in data:
                    continue
                for line_number, text in enumerate(
                    data.decode("utf-8", errors="replace").splitlines(), start=1
                ):
                    haystack = text if case_sensitive else text.casefold()
                    if needle not in haystack:
                        continue
                    matches.append(
                        {
                            "path": str(resolved.relative_to(root)),
                            "line": line_number,
                            "text": text[:1000],
                        }
                    )
                    if len(matches) >= limit:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def git(
        self,
        project: Project,
        *,
        action: str,
        limit: int = 20,
        revision: str | None = None,
    ) -> dict[str, str]:
        self._validate_limit(limit)
        root = self._project_root(project)
        base = [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(root),
        ]
        if action == "status":
            command = [*base, "status", "--short", "--branch", "--untracked-files=normal"]
        elif action == "log":
            command = [
                *base,
                "log",
                f"--max-count={limit}",
                "--date=iso-strict",
                "--pretty=format:%h %ad %s",
            ]
        elif action == "diff":
            command = [*base, "diff", "--stat", "--no-ext-diff", "--no-textconv"]
        elif action == "show":
            if (
                not isinstance(revision, str)
                or revision.startswith("-")
                or _REVISION.fullmatch(revision) is None
            ):
                raise ValueError("project git revision is invalid")
            command = [
                *base,
                "show",
                "--stat",
                "--oneline",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                revision,
            ]
        else:
            raise ValueError("project git action must be status, log, diff, or show")

        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or "git command failed"
            raise RuntimeError(message[:2000])
        return {"action": action, "output": completed.stdout[: 64 * 1024]}

    @staticmethod
    def _project_root(project: Project) -> Path:
        root = project.path.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise RuntimeError(f"project path is not a directory: {project.path}")
        return root

    @classmethod
    def _resolve(cls, root: Path, path: str) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("project path must be a non-empty relative path")
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise PermissionError("project path escapes the authorized project")
        resolved = (root / relative).resolve(strict=True)
        cls._assert_safe(root, resolved)
        return resolved

    @classmethod
    def _assert_safe(cls, root: Path, resolved: Path) -> None:
        if not resolved.is_relative_to(root):
            raise PermissionError("project path escapes the authorized project")
        relative = resolved.relative_to(root)
        if any(cls._denied_name(part) for part in relative.parts):
            raise PermissionError("project path is protected")

    @staticmethod
    def _denied_name(name: str) -> bool:
        return name == ".git" or name == ".env" or name.startswith(".env.")

    @staticmethod
    def _validate_pattern(pattern: str) -> None:
        if (
            not isinstance(pattern, str)
            or not pattern
            or Path(pattern).is_absolute()
            or ".." in Path(pattern).parts
        ):
            raise ValueError("project glob pattern is invalid")

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_RESULTS:
            raise ValueError(f"project result limit must be between 1 and {_MAX_RESULTS}")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
