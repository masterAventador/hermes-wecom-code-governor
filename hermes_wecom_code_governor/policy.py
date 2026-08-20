from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Identity:
    user_id: str
    chat_id: str
    chat_type: str


@dataclass(frozen=True)
class RemoteAction:
    """预登记的远程动作：固定 ssh 主机 + 固定命令，模型只能按名称触发。"""

    name: str
    host: str
    argv: tuple[str, ...]
    timeout_seconds: int = 30


@dataclass(frozen=True)
class Project:
    project_id: str
    display_name: str
    path: Path
    base_branch: str | None = None
    validation_commands: tuple[tuple[str, ...], ...] = ()
    seed_paths: tuple[str, ...] = ()
    readable_paths: tuple[Path, ...] = ()
    job_allowed_commands: tuple[tuple[str, ...], ...] = ()
    job_gui_commands: tuple[tuple[str, ...], ...] = ()
    job_environment: tuple[tuple[str, str], ...] = ()
    job_artifact_globs: tuple[str, ...] = ()
    job_timeout_seconds: int = 1800
    job_home_seeds: tuple[tuple[Path, Path], ...] = ()
    job_unix_sockets: tuple[Path, ...] = ()
    remote_actions: tuple[RemoteAction, ...] = ()
    push_on_merge: bool = False
    auto_discovered: bool = False


@dataclass(frozen=True)
class PermissionGroup:
    name: str
    user_ids: frozenset[str]
    chat_ids: frozenset[str]
    project_ids: frozenset[str] = field(default_factory=frozenset)
    root_paths: tuple[Path, ...] = ()

    def matches(self, identity: Identity) -> bool:
        return identity.user_id in self.user_ids and (
            "*" in self.chat_ids or identity.chat_id in self.chat_ids
        )

    def grants(self, project: Project) -> bool:
        if "*" in self.project_ids or project.project_id in self.project_ids:
            return True
        project_path = project.path.resolve()
        return any(project_path.is_relative_to(root.resolve()) for root in self.root_paths)


@dataclass(frozen=True)
class Policy:
    projects: tuple[Project, ...]
    permission_groups: tuple[PermissionGroup, ...]

    def matches_identity(self, identity: Identity) -> bool:
        return any(group.matches(identity) for group in self.permission_groups)

    def authorized_project_ids(self, identity: Identity) -> tuple[str, ...]:
        matching_groups = tuple(
            group for group in self.permission_groups if group.matches(identity)
        )
        if not matching_groups:
            return ()
        return tuple(
            sorted(
                project.project_id
                for project in self.projects
                if any(group.grants(project) for group in matching_groups)
            )
        )

    def is_authorized(self, identity: Identity) -> bool:
        return bool(self.authorized_project_ids(identity))

    def project(self, project_id: str) -> Project:
        for project in self.projects:
            if project.project_id == project_id:
                return project
        raise KeyError(project_id)
