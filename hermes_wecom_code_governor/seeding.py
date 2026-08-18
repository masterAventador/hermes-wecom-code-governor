from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath


def require_safe_seed_paths(seed_paths: tuple[str, ...]) -> None:
    for seed_path in seed_paths:
        pure = PurePosixPath(seed_path)
        if pure.is_absolute() or ".." in pure.parts or ".git" in pure.parts:
            raise PermissionError("project seed path must stay inside the repository")


def seed_workspace(
    repo: Path,
    workspace: Path,
    seed_paths: tuple[str, ...],
    *,
    skip_existing: bool = False,
) -> None:
    resolved_repo = repo.resolve()
    for relative in seed_paths:
        source = (resolved_repo / relative).resolve(strict=True)
        if not source.is_relative_to(resolved_repo):
            raise PermissionError("project seed path must stay inside the repository")
        copy_seed(source, workspace / relative, skip_existing=skip_existing)


def copy_seed(source: Path, destination: Path, *, skip_existing: bool = False) -> None:
    if destination.exists():
        if skip_existing:
            return
        raise RuntimeError(f"seed destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
        return
    if source.is_file():
        shutil.copy2(source, destination)
        return
    raise ValueError(f"seed must be a file or directory: {source}")
