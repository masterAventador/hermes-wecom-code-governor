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
class HttpActionParameter:
    """受控 HTTP 动作的参数规格：integer 带上下界，choice 只认枚举值。"""

    name: str
    type: str
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    # 取值本身往往说不清语义（闪烁频率 16 和 0.25 哪个算"快闪"），这句中文说明
    # 会跟着参数进模型上下文。
    description: str = ""


@dataclass(frozen=True)
class HttpAction:
    """预登记的受控 HTTP 动作：URL/请求体是配置里的固定模板，模型只能按名称
    触发并提供白名单参数，参数校验通过后填入模板占位符。"""

    name: str
    method: str
    url: str
    body_template: str | None = None
    parameters: tuple[HttpActionParameter, ...] = ()
    timeout_seconds: int = 15


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
    # 受信命令：无 seatbelt、HOME/TMPDIR 用真实值直接执行（macOS 钥匙串签名
    # 与写限制沙箱互斥，且登录钥匙串签名依赖真实 HOME）。仅保留隔离 worktree
    # 作为 cwd 与环境白名单，文件系统无限制——等价于用户亲自在终端执行；
    # 仅登记确需签名/公证的打包命令。
    job_trusted_commands: tuple[tuple[str, ...], ...] = ()
    job_environment: tuple[tuple[str, str], ...] = ()
    # 仅受信命令可见的环境变量与 HOME 种子：签名证书、密钥密码等敏感材料
    # 放这里，普通沙箱任务（测试/截图）不装配、也拿不到。
    job_trusted_environment: tuple[tuple[str, str], ...] = ()
    job_artifact_globs: tuple[str, ...] = ()
    job_timeout_seconds: int = 1800
    job_home_seeds: tuple[tuple[Path, Path], ...] = ()
    job_trusted_home_seeds: tuple[tuple[Path, Path], ...] = ()
    job_unix_sockets: tuple[Path, ...] = ()
    remote_actions: tuple[RemoteAction, ...] = ()
    http_actions: tuple[HttpAction, ...] = ()
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
