from __future__ import annotations

import hashlib
import os
import re
from configparser import ConfigParser
from configparser import Error as ConfigParserError
from dataclasses import dataclass
from pathlib import Path

_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".cache",
        ".dart_tool",
        ".git",
        ".gradle",
        ".runtime",
        ".venv",
        "Pods",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "out",
        "target",
        "venv",
        "worktrees",
        "wt",
    }
)
_PROJECT_ID_CHARACTER = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class DiscoveredRepository:
    path: Path
    discovery_root: Path

    @property
    def display_name(self) -> str:
        relative = self.path.relative_to(self.discovery_root)
        return self.discovery_root.name if relative == Path(".") else relative.as_posix()

    @property
    def project_id(self) -> str:
        slug = _PROJECT_ID_CHARACTER.sub("-", self.path.name.casefold()).strip("-")
        slug = slug[:32].rstrip("-") or "repository"
        digest = hashlib.sha256(str(self.path).encode("utf-8")).hexdigest()[:10]
        return f"auto-{slug}-{digest}"


def _is_within_any(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _initialized_submodules(
    repository: Path,
    authorized_root: Path,
    excluded_paths: tuple[Path, ...],
) -> tuple[Path, ...]:
    modules_path = repository / ".gitmodules"
    if not modules_path.is_file():
        return ()
    parser = ConfigParser(interpolation=None)
    try:
        parser.read(modules_path, encoding="utf-8")
    except (ConfigParserError, OSError, UnicodeError):
        return ()
    submodules: list[Path] = []
    for section in parser.sections():
        raw_path = parser.get(section, "path", fallback="").strip()
        if not raw_path:
            continue
        unresolved = repository / raw_path
        if unresolved.is_symlink():
            continue
        candidate = unresolved.resolve()
        if not candidate.is_relative_to(authorized_root):
            continue
        if _is_within_any(candidate, excluded_paths):
            continue
        if (candidate / ".git").exists():
            submodules.append(candidate)
    return tuple(submodules)


def discover_git_repositories(
    roots: tuple[Path, ...],
    *,
    excluded_paths: tuple[Path, ...] = (),
    max_projects: int = 500,
) -> tuple[DiscoveredRepository, ...]:
    """Discover Git working trees below authorized roots without following symlinks."""
    resolved_exclusions = tuple(path.resolve() for path in excluded_paths)
    resolved_roots = tuple(
        sorted(
            {path.resolve() for path in roots if path.is_dir()},
            key=lambda path: (len(path.parts), str(path)),
        )
    )
    repositories: dict[Path, Path] = {}

    def record_repository(path: Path, root: Path) -> None:
        pending = [path]
        while pending:
            repository = pending.pop()
            if repository in repositories:
                continue
            repositories[repository] = root
            if len(repositories) > max_projects:
                raise ValueError(
                    f"project discovery found more than {max_projects} Git repositories"
                )
            pending.extend(_initialized_submodules(repository, root, resolved_exclusions))

    for root in resolved_roots:
        if _is_within_any(root, resolved_exclusions):
            continue
        for current_text, directory_names, file_names in os.walk(root, followlinks=False):
            current = Path(current_text).resolve()
            if _is_within_any(current, resolved_exclusions):
                directory_names[:] = []
                continue

            has_git_marker = ".git" in directory_names or ".git" in file_names
            if has_git_marker:
                record_repository(current, root)
                directory_names[:] = []
                continue

            retained_directories: list[str] = []
            for name in directory_names:
                if name in _IGNORED_DIRECTORY_NAMES:
                    continue
                unresolved = current / name
                if unresolved.is_symlink():
                    continue
                candidate = unresolved.resolve()
                if _is_within_any(candidate, resolved_exclusions):
                    continue
                retained_directories.append(name)
            directory_names[:] = retained_directories

    return tuple(
        DiscoveredRepository(path, repositories[path]) for path in sorted(repositories, key=str)
    )
