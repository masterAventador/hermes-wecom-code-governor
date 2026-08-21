from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .codex_runtime import (
    CodexAppServerRunner,
    CodexMode,
    CodexRunner,
    CodexRunRequest,
    CodexTaskState,
)
from .config import GovernorConfig, build_project_catalog
from .delivery import ArtifactDelivery, FileDeliveryService
from .execution import combine_output
from .http_action import HttpActionRunner
from .policy import HttpAction, HttpActionParameter, Identity, Policy, Project
from .project_inspector import ProjectInspector
from .project_job import ProjectJobRunner
from .state import DurableState, SessionRecord, SessionStateRepository
from .worktree import CompletionStatus, SafetyLimits, WorktreeManager

logger = logging.getLogger(__name__)

_SEPARATOR_RUN = re.compile(r"-+")
_GOVERNOR_TOOLS = frozenset(
    {
        "governor_list_projects",
        "governor_select_project",
        "governor_project_files",
        "governor_project_read",
        "governor_project_search",
        "governor_project_git",
        "governor_codex_change",
        "governor_project_job",
        "governor_remote_task",
        "governor_http_action",
        "governor_push",
        "governor_deliver_file",
    }
)
_GOVERNED_BUILTIN_TOOLS = frozenset(
    {"read_file", "write_file", "patch", "search_files", "terminal"}
)
_SAFE_CONVERSATION_TOOLS = frozenset(
    {"clarify", "web_search", "web_extract", "vision_analyze", "video_analyze", "todo"}
)


@dataclass(frozen=True)
class SessionEnvironment:
    platform: str
    session_key: str
    identity: Identity
    message_id: str = ""


def build_task_id(title: str, *, month: int, day: int, max_title_chars: int = 20) -> str:
    semantic: list[str] = []
    separator_pending = False
    for character in title.strip():
        if character.isalnum() or character in {"_", "."}:
            if separator_pending and semantic:
                semantic.append("-")
            semantic.append(character)
            separator_pending = False
        else:
            separator_pending = True
    normalized = _SEPARATOR_RUN.sub("-", "".join(semantic)).strip("-.")
    normalized = normalized[:max_title_chars].rstrip("-.")
    if not normalized:
        raise ValueError("task title must contain meaningful letters or numbers")
    return f"{month:02d}{day:02d}-{normalized}"


class GovernorRuntime:
    def __init__(
        self,
        config: GovernorConfig,
        state: DurableState,
        *,
        env_provider: Callable[[], SessionEnvironment],
        worktrees: WorktreeManager | Any | None = None,
        codex: CodexRunner | None = None,
        delivery: FileDeliveryService | Any | None = None,
        jobs: ProjectJobRunner | Any | None = None,
        inspector: ProjectInspector | Any | None = None,
        remote: Any | None = None,
        http: Any | None = None,
        notifier: Callable[[Identity, str], None] | None = None,
        now: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        self.config = config
        self._env_provider = env_provider
        self._explicit_projects = tuple(
            project for project in config.policy.projects if not project.auto_discovered
        )
        self._projects = {project.project_id: project for project in config.policy.projects}
        self._state = SessionStateRepository(state, self._projects)
        self._worktrees = worktrees or WorktreeManager(config.runtime_root)
        self._codex = codex or CodexAppServerRunner(config.codex)
        self._delivery = delivery
        self._jobs = jobs or ProjectJobRunner(
            config.runtime_root,
            codex_binary=config.codex.codex_bin,
        )
        self._inspector = inspector or ProjectInspector()
        if remote is None:
            from .remote import SshRemoteRunner

            remote = SshRemoteRunner()
        self._remote = remote
        self._http = http or HttpActionRunner()
        self._notifier = notifier
        self._pending_deliveries: dict[str, deque[ArtifactDelivery]] = defaultdict(deque)
        # 工具被拒或超时后外层会带同一条消息重试；按 (通知类别, 会话) 记录已提示
        # 的 message_id 去重，避免用户收到重复的"请稍候"提示。
        self._notice_messages: dict[tuple[str, str], str] = {}
        if now is None:
            from datetime import datetime

            def current_month_day() -> tuple[int, int]:
                current = datetime.now()
                return current.month, current.day

            now = current_month_day
        self._now = now

    def set_notifier(self, notifier: Callable[[Identity, str], None]) -> None:
        self._notifier = notifier

    def _notify(self, identity: Identity, message: str) -> bool:
        if self._notifier is None:
            return False
        try:
            self._notifier(identity, message)
        except Exception:
            return False
        return True

    def _notify_once(self, env: SessionEnvironment, kind: str, message: str) -> None:
        """同一条触发消息只发一次某类提示；无 message_id 时退化为每次都发。"""
        key = (kind, env.session_key)
        if env.message_id and self._notice_messages.get(key) == env.message_id:
            return
        # 发送失败不记"已提示"，让同一条消息的下次重试把提示补上。
        if self._notify(env.identity, message) and env.message_id:
            self._notice_messages[key] = env.message_id

    def pre_gateway_dispatch(self, event: object, **_: Any) -> dict[str, str]:
        source = getattr(event, "source", None)
        platform = self._platform_name(getattr(source, "platform", ""))
        if platform != "wecom":
            return {"action": "allow"}
        identity = Identity(
            user_id=str(getattr(source, "user_id", "") or ""),
            chat_id=str(getattr(source, "chat_id", "") or ""),
            chat_type=str(getattr(source, "chat_type", "") or ""),
        )
        if not self._identity_is_authorized(identity):
            return {"action": "skip", "reason": "not authorized by code governor"}
        message_id = str(getattr(event, "message_id", "") or "").strip()
        if message_id and not getattr(source, "message_id", None):
            source.message_id = message_id
        return {"action": "allow"}

    def pre_llm_call(self, **_: Any) -> dict[str, str] | None:
        env = self._env_provider()
        if env.platform != "wecom" or not self._identity_is_authorized(env.identity):
            return None
        record = self._record(env)
        if record.project_id:
            project = self._projects.get(record.project_id)
            if project is None or not (project.path / ".git").exists():
                self._refresh_projects()
                record = self._record(env)
        # The platform adapter runs on the gateway event loop, outside the
        # model worker's task-local ContextVars. Persist the exact identity so
        # a later project-card callback can be correlated without parsing a
        # Hermes session key or trusting callback-supplied identity alone.
        self._state.save(env.session_key, record)
        if record.project_id:
            selected = self._projects[record.project_id]
            current = f"当前项目：{selected.display_name}（{selected.project_id}）。"
        else:
            current = "当前尚未选择项目。"
        if record.active_worktree:
            current += (
                f" 当前有活动 worktree：{record.active_worktree.path}，"
                f"任务：{record.active_worktree.task_id}。"
            )
        selected_job_lines: list[str] = []
        selected_remote_lines: list[str] = []
        selected_http_lines: list[str] = []
        if record.project_id:
            selected_project = self._projects[record.project_id]
            selected_job_lines = [
                f"- {' '.join(command)}" for command in selected_project.job_allowed_commands
            ]
            if selected_job_lines:
                selected_job_lines.insert(0, "当前项目允许的受控本地任务命令：")
            selected_remote_lines = [
                f"- {action.name}" for action in selected_project.remote_actions
            ]
            if selected_remote_lines:
                selected_remote_lines.insert(0, "当前项目可触发的远程受控动作（按名称触发）：")
            selected_http_lines = [
                self._http_action_line(action) for action in selected_project.http_actions
            ]
            if selected_http_lines:
                selected_http_lines.insert(
                    0, "当前项目可触发的受控 HTTP 动作（按名称触发，参数只能取下列取值）："
                )

        context = "\n".join(
            (
                "[代码治理插件上下文]",
                "介绍自己或回答身份相关问题时可以自然表达，措辞由你自己把握，不必套用固定话术。"
                "唯一的硬约束是：不要透露真实模型、背后的厂商或底层技术栈，被追问时礼貌地说"
                "这属于不便透露的信息即可。",
                "",
                "先判断当前请求是否需要访问具体项目；普通聊天、自我介绍、"
                "通用知识问答不需要选择项目，直接简洁回答。不要使用关键词路由，也不要把所有请求"
                "硬塞进固定的三种分支。用户提到项目名称或路径但不能直接确定项目时，先调用 "
                "governor_list_projects 搜索；唯一匹配可直接选择，多个匹配时才调用 clarify。"
                "搜索结果的 match_kind 为 exact 时，必须优先使用精确匹配，不得因为同时存在名称"
                "更长的子项目而弹卡片。"
                "项目卡片最多放 6 个候选，choices 只能使用搜索结果中的授权项目显示名称。"
                "开放式追问使用普通文本，不弹项目卡片。",
                "",
                "回复必须以用户问题为边界：只回答用户明确提出的问题，能用一句话答完就只答"
                "一句，不主动扩展背景、建议或内部过程。除非用户明确询问，否则不要展示任务"
                "状态、退出码、基准提交、文件大小、执行日志或存储渠道。成功交付文件时，默认"
                "只保留一句直接结果和一个下载入口；下载入口由企微交付层自动追加，不要复述"
                "工具返回的下载地址。",
                "",
                "可用工具只有以 governor_ 开头的治理工具和基础会话工具"
                "（clarify、web_search、web_extract、vision_analyze、video_analyze、todo）。"
                "不要尝试调用任何其他工具（如 tool_describe、session_search、文件或终端工具），"
                "它们会被直接拦截，只会浪费时间。",
                "",
                current,
                *selected_job_lines,
                *selected_remote_lines,
                *selected_http_lines,
                "",
                "用户明确提到另一个授权项目时，可以调用 governor_select_project 切换，不要被旧"
                "项目记忆绑死。项目只读查询由你直接使用 governor_project_files、"
                "governor_project_read、governor_project_search、governor_project_git 核验后回答；"
                "优先一次调用取得足够证据，证据足够立即回答。只有修改代码才调用 "
                "governor_codex_change，title 由你概括为不超过 12 个字的语义名称。收到修改工具"
                "结果后，先用一行引用用户这次的原始需求原话（Markdown 引用，格式如"
                "『> 需求：<把用户这条消息原样抄下来>』），再另起一行以 answer 为主说明你的改动，"
                "方便用户核对你的理解与需求是否一致；引用要逐字照抄用户原话，不要改写或概括。"
                "并准确说明返回状态。合并成功后若项目开启了自动推送会一并推送远端；返回的 "
                "message 非空表示推送失败，此时要如实告诉用户“已合并到本地，但推送远端失败”，"
                "不要谎称已推送。用户明确要求推送时（如让你把本地提交推到远端），调用 "
                "governor_push；项目未开启推送权限时它会拒绝，如实转告即可。",
                "用户明确要求打包、测试、导出、生成产物等不修改源码的本地动作时，使用 "
                "governor_project_job；只有修改源码才使用 governor_codex_change。任务声明了产物"
                "时会自动完成交付，不要重复调用文件交付工具。发送产物下载链接时，"
                "链接的显示文字用中文描述（含平台与版本，如『下载 Mac 安装包 (v1.2.6)』），"
                "不要把原始文件名当链接文案。",
                "用户明确要求把当前项目中的现有文件发给他时，调用 governor_deliver_file；不要"
                "在用户未要求交付文件时调用。",
                "当前项目登记了远程受控动作时（见上方清单），用户明确要求执行其中某个动作，"
                "就用 governor_remote_task 并传入清单里的动作名称；动作的目标主机与命令都是"
                "预先固定的，你只能按名称触发，不能自行构造命令或主机，也不要触发用户未要求的动作。",
                "用户要求控制警示灯、查看灯或设备连接状态等已登记的 HTTP 动作时（见上方清单），"
                "使用 governor_http_action，只能传清单里列出的动作名称与参数取值；请求地址和请求体"
                "都由配置固定，你不能自行拼接 URL，也不要触发用户未要求的动作。",
                "调用 governor_codex_change、governor_project_job、governor_remote_task 这类耗时"
                "工具时，请通过 ack 参数先给用户一句回复，告诉他你接下来要做什么；内容和语气由你"
                "自己组织，回应用户这次说的话就好，别每次都用同一个句式。",
                "",
                "默认行为只是分析或修改代码。打包、上传或部署只有在用户当前请求明确要求且存在"
                "对应受控工具时才允许；绝不能自行打包、上传、部署、推送或发布。无法理解具体要做"
                "什么时再追问用户，不要假装已经执行。",
                "[/代码治理插件上下文]",
            )
        )
        return {"context": context}

    def pre_tool_call(self, tool_name: str, args: dict | None = None, **_: Any) -> dict | None:
        env = self._env_provider()
        if env.platform != "wecom":
            return None
        if not self._identity_is_authorized(env.identity):
            return {"action": "block", "message": "blocked: unauthorized session"}
        if tool_name in _GOVERNOR_TOOLS:
            return None
        if tool_name in _SAFE_CONVERSATION_TOOLS:
            return None
        if tool_name in _GOVERNED_BUILTIN_TOOLS:
            return {
                "action": "block",
                "message": "blocked: project access must run through code governor tools",
            }
        return {
            "action": "block",
            "message": f"blocked: tool {tool_name!r} is not enabled for the code bot",
        }

    def list_projects(self, *, query: str = "", limit: int = 20) -> dict[str, Any]:
        env = self._require_authorized_environment()
        self._refresh_projects()
        if limit < 1 or limit > 50:
            raise ValueError("project list limit must be between 1 and 50")
        normalized_query = query.strip().casefold()
        projects = self._authorized_projects(env.identity)
        match_kind = "all"
        if normalized_query:
            exact_projects = tuple(
                project
                for project in projects
                if any(
                    normalized_query == value.casefold()
                    for value in (
                        project.project_id,
                        project.display_name,
                        str(project.path),
                        project.path.name,
                    )
                )
            )
            if exact_projects:
                projects = exact_projects
                match_kind = "exact"
            else:
                projects = tuple(
                    project
                    for project in projects
                    if any(
                        normalized_query in value.casefold()
                        for value in (
                            project.project_id,
                            project.display_name,
                            str(project.path),
                        )
                    )
                )
                match_kind = "contains"
        total = len(projects)
        return {
            "projects": [
                {
                    "project_id": project.project_id,
                    "display_name": project.display_name,
                    "path": str(project.path),
                }
                for project in projects[:limit]
            ],
            "total": total,
            "truncated": total > limit,
            "match_kind": match_kind,
        }

    def is_authorized_identity(self, identity: Identity) -> bool:
        return self._identity_is_authorized(identity)

    def select_project(self, project_value: str) -> dict[str, str]:
        env = self._require_authorized_environment()
        if self._state.load(env.session_key) is None:
            self._state.save(env.session_key, SessionRecord(env.identity))
        return self.select_project_for_session(env.session_key, env.identity, project_value)

    def project_choices_for_session(self, session_key: str) -> dict[str, str]:
        self._refresh_projects()
        record = self.session_record_for_adapter(session_key)
        if record is None or not self._identity_is_authorized(record.identity):
            return {}
        return {
            project.project_id: project.display_name
            for project in self._authorized_projects(record.identity)
        }

    def session_record_for_adapter(self, session_key: str) -> SessionRecord | None:
        record = self._state.load(session_key)
        if record is None or not self._identity_is_authorized(record.identity):
            return None
        return record

    def select_project_for_session(
        self,
        session_key: str,
        identity: Identity,
        project_value: str,
    ) -> dict[str, str]:
        self._refresh_projects()
        prior = self._state.load(session_key)
        if prior is None or not self._shares_session_scope(prior.identity, identity):
            raise PermissionError("project card does not belong to this chat session")
        if not self._identity_is_authorized(identity):
            raise PermissionError("session identity is not authorized")
        project = self._resolve_authorized_project(identity, project_value)
        if prior.active_worktree and prior.project_id != project.project_id:
            raise RuntimeError("cannot switch project while a worktree task is active")
        self._state.save(
            session_key,
            SessionRecord(
                identity,
                project.project_id,
                prior.active_worktree,
                prior.active_task_thread_id,
            ),
        )
        return {"project_id": project.project_id, "display_name": project.display_name}

    def begin_task(self, title: str) -> dict[str, str]:
        env = self._require_authorized_environment()
        self._refresh_projects()
        record = self._record(env)
        if record.project_id is None:
            raise RuntimeError("select an authorized project before starting a task")
        if record.active_worktree is not None:
            raise RuntimeError(f"an active task already exists: {record.active_worktree.task_id}")
        month, day = self._now()
        task_id = build_task_id(title, month=month, day=day)
        active = self._worktrees.begin(self._projects[record.project_id], task_id)
        self._state.save(
            env.session_key,
            SessionRecord(
                env.identity,
                record.project_id,
                active,
                None,
            ),
        )
        return {
            "task_id": active.task_id,
            "project_id": active.project.project_id,
            "worktree_path": str(active.path),
            "base_branch": active.base_branch,
            "branch_name": active.branch_name,
        }

    def complete_task(self) -> dict[str, str | None]:
        env = self._require_authorized_environment()
        record = self._record(env)
        if record.active_worktree is None:
            raise RuntimeError("there is no active worktree task")
        result = self._worktrees.complete(
            record.active_worktree,
            SafetyLimits(
                max_changed_files=self.config.safety.max_changed_files,
                max_deleted_files=self.config.safety.max_deleted_files,
            ),
        )
        if result.status in {CompletionStatus.MERGED, CompletionStatus.NO_CHANGES}:
            self._state.save(
                env.session_key,
                SessionRecord(
                    env.identity,
                    record.project_id,
                    None,
                    None,
                ),
            )
        return {
            "status": result.status.value,
            "commit": result.commit,
            "message": result.message,
        }

    def project_files(
        self,
        *,
        path: str = ".",
        pattern: str = "*",
        recursive: bool = False,
        limit: int = 50,
        sort: str = "path",
        sha256: bool = False,
    ) -> dict[str, Any]:
        project = self._selected_project_for_read()
        result = self._inspector.files(
            project,
            path=path,
            pattern=pattern,
            recursive=recursive,
            limit=limit,
            sort=sort,
            sha256=sha256,
        )
        return {**result, "project": project.display_name}

    def project_read(
        self,
        *,
        paths: list[str],
        start_line: int = 1,
        max_lines: int = 200,
    ) -> dict[str, Any]:
        project = self._selected_project_for_read()
        result = self._inspector.read(
            project,
            paths=paths,
            start_line=start_line,
            max_lines=max_lines,
        )
        return {**result, "project": project.display_name}

    def project_search(
        self,
        *,
        query: str,
        path: str = ".",
        patterns: list[str] | None = None,
        limit: int = 50,
        case_sensitive: bool = False,
    ) -> dict[str, Any]:
        project = self._selected_project_for_read()
        result = self._inspector.search(
            project,
            query=query,
            path=path,
            patterns=patterns,
            limit=limit,
            case_sensitive=case_sensitive,
        )
        return {**result, "project": project.display_name}

    def project_git(
        self,
        *,
        action: str,
        limit: int = 20,
        revision: str | None = None,
    ) -> dict[str, Any]:
        project = self._selected_project_for_read()
        result = self._inspector.git(
            project,
            action=action,
            limit=limit,
            revision=revision,
        )
        return {**result, "project": project.display_name}

    def _selected_project(
        self,
        *,
        action: str,
        require_idle: bool,
    ) -> tuple[SessionEnvironment, Project]:
        """已授权环境 + 当前已选项目的公共前导，供只读、本地任务、远程动作复用。

        require_idle=True 时，存在活动改码 worktree 会拒绝（这些动作与改码互斥）；
        远程动作不碰本地仓库，但仍要求先结束改码任务，避免同一会话状态交错。
        """
        env = self._require_authorized_environment()
        self._refresh_projects()
        record = self._record(env)
        if record.project_id is None:
            raise RuntimeError(f"select an authorized project before {action}")
        if require_idle and record.active_worktree is not None:
            raise RuntimeError(f"finish the active code task before {action}")
        return env, self._projects[record.project_id]

    def _selected_project_for_read(self) -> Project:
        _, project = self._selected_project(action="reading code", require_idle=True)
        return project

    @staticmethod
    def _ack_or(ack: str | None, fallback: str) -> str:
        """模型自拟的开工回复优先；没写（或全空白）才用模板句兜底。"""
        text = (ack or "").strip()
        return text if text else fallback

    def codex_change(
        self, request: str, title: str, ack: str | None = None
    ) -> dict[str, str | None]:
        env = self._require_authorized_environment()
        self._refresh_projects()
        record = self._record(env)
        if record.project_id is None:
            raise RuntimeError("select an authorized project before changing code")
        resumed = record.active_worktree is not None
        if record.active_worktree is None:
            self.begin_task(title)
            record = self._record(env)
        active = record.active_worktree
        if active is None:
            raise RuntimeError("failed to create an active worktree")
        if resumed:
            self._worktrees.ensure_seeded(active)
        readable, writable = self._worktrees.codex_roots(active)
        self._notify_once(
            env,
            "codex",
            self._ack_or(ack, f"正在修改 {active.project.display_name}，请稍候。"),
        )
        result = self._codex.run(
            CodexRunRequest(
                mode=CodexMode.WRITE,
                prompt=request,
                cwd=active.path,
                thread_id=record.active_task_thread_id,
                readable_roots=(*readable, *active.project.readable_paths),
                writable_roots=writable,
            )
        )
        self._state.save(
            env.session_key,
            SessionRecord(
                env.identity,
                record.project_id,
                active,
                result.thread_id,
            ),
        )
        if result.task_state is CodexTaskState.NEEDS_INPUT:
            return {
                "answer": result.answer,
                "status": "needs_input",
                "project": active.project.display_name,
                "commit": None,
                "message": "",
            }
        completion = self._worktrees.complete(
            active,
            SafetyLimits(
                max_changed_files=self.config.safety.max_changed_files,
                max_deleted_files=self.config.safety.max_deleted_files,
            ),
        )
        if completion.status in {CompletionStatus.MERGED, CompletionStatus.NO_CHANGES}:
            self._state.save(
                env.session_key,
                SessionRecord(
                    env.identity,
                    record.project_id,
                    None,
                    None,
                ),
            )
        return {
            "answer": result.answer,
            "status": completion.status.value,
            "project": active.project.display_name,
            "commit": completion.commit,
            "message": completion.message,
        }

    def project_job(
        self,
        *,
        argv: list[str],
        artifact_globs: list[str] | None = None,
        title: str,
        ack: str | None = None,
    ) -> dict[str, Any]:
        env = self._require_authorized_environment()
        project = self._selected_project_for_job()
        if not env.message_id:
            raise RuntimeError("project job requires the current WeCom message id")
        normalized_title = title.strip()
        if not normalized_title or len(normalized_title) > 12:
            raise ValueError("job title must contain 1 to 12 characters")
        command = tuple(argv)
        # 模型不传产物 glob 时回退到项目登记的全部产物（宽松模式：任务没产出
        # 该类产物不算失败），避免打包类任务成功却无交付、测试类任务被误伤。
        explicit_artifacts = bool(artifact_globs)
        requested_artifacts = (
            tuple(artifact_globs) if explicit_artifacts else project.job_artifact_globs
        )
        if self._delivery is None:
            if explicit_artifacts:
                raise RuntimeError("file delivery is not configured for generated artifacts")
            requested_artifacts = ()
        month, day = self._now()
        task_id = build_task_id(normalized_title, month=month, day=day)
        message_suffix = hashlib.sha256(env.message_id.encode("utf-8")).hexdigest()[:10]
        job_id = f"{task_id}--{message_suffix}"
        # 先校验再提示：参数被拒的调用不应对用户放出"请稍候"。
        self._jobs.validate(
            project,
            job_id=job_id,
            argv=command,
            artifact_globs=requested_artifacts,
        )
        self._notify_once(
            env,
            "job",
            self._ack_or(ack, f"正在为 {project.display_name}执行“{normalized_title}”，请稍候。"),
        )
        result = self._jobs.run(
            project,
            job_id=job_id,
            argv=command,
            artifact_globs=requested_artifacts,
            require_artifacts=explicit_artifacts,
        )
        deliveries: list[dict[str, str | int | None]] = []
        if result.status == "completed" and result.artifacts:
            if self._delivery is None or result.staging_root is None:
                raise RuntimeError("generated artifacts are missing a delivery staging root")
            for artifact in result.artifacts:
                delivery = self._delivery.prepare_staged(
                    artifact,
                    result.staging_root,
                    env.message_id,
                )
                self._pending_deliveries[delivery.message_id].append(delivery)
                deliveries.append(
                    {
                        "channel": delivery.channel,
                        "filename": delivery.filename,
                        "size_bytes": delivery.size_bytes,
                        "download_url": delivery.download_url,
                    }
                )
        reply: dict[str, Any] = {
            "task_id": task_id,
            "status": result.status,
            "exit_code": result.exit_code,
            "output": result.output,
            "base_commit": result.base_commit[:7],
            "artifacts": deliveries,
        }
        if result.status == "completed" and requested_artifacts and not result.artifacts:
            # 宽松模式下命令成功却零产出（例如打包配置漂移）不能被说成干净的成功。
            reply["warning"] = (
                "任务成功结束，但没有产出任何已登记的交付产物"
                f"（预期匹配：{', '.join(requested_artifacts)}）。"
                "必须向用户如实说明没有可交付文件。"
            )
        return reply

    def push_remote(self) -> dict[str, Any]:
        env, project = self._selected_project(action="pushing to the remote", require_idle=True)
        if not project.push_on_merge:
            raise PermissionError("push is not enabled for this project")
        logger.info(
            "governed push requested: project=%s user=%s chat=%s",
            project.project_id,
            env.identity.user_id,
            env.identity.chat_id,
        )
        error = self._worktrees.push_base(project)
        logger.info(
            "governed push finished: project=%s user=%s ok=%s",
            project.project_id,
            env.identity.user_id,
            error is None,
        )
        return {
            "project": project.display_name,
            "status": "pushed" if error is None else "failed",
            "message": combine_output(error or "", ""),
        }

    def remote_task(self, action_name: str, ack: str | None = None) -> dict[str, Any]:
        env, project = self._selected_project(action="running a remote action", require_idle=True)
        name = action_name.strip()
        action = next((item for item in project.remote_actions if item.name == name), None)
        if action is None:
            raise PermissionError("remote action is not registered for this project")
        # 发放许可证类动作必须可追溯：记录谁、在哪个会话、触发了哪个动作。
        logger.info(
            "governed remote action requested: action=%s project=%s user=%s chat=%s",
            action.name,
            project.project_id,
            env.identity.user_id,
            env.identity.chat_id,
        )
        self._notify_once(
            env,
            "remote",
            self._ack_or(ack, f"正在为 {project.display_name}执行“{action.name}”，请稍候。"),
        )
        result = self._remote.run(action)
        succeeded = result.exit_code == 0
        logger.info(
            "governed remote action finished: action=%s user=%s exit=%s",
            action.name,
            env.identity.user_id,
            result.exit_code,
        )
        # 成功时结果只取 stdout（避免把 ssh 的 stderr 警告当成激活码回给用户）；
        # 失败时合并 stdout/stderr 供诊断，两者都截断防止淹没上下文并转发进群。
        if succeeded:
            output = combine_output(result.stdout, "")
        else:
            output = combine_output(result.stdout, result.stderr)
        return {
            "action": action.name,
            "status": "completed" if succeeded else "failed",
            "exit_code": result.exit_code,
            "output": output,
        }

    @staticmethod
    def _http_parameter_summary(parameter: HttpActionParameter) -> str:
        if parameter.type == "integer":
            return f"{parameter.name}: {parameter.minimum}-{parameter.maximum} 的整数"
        return f"{parameter.name}: {'/'.join(parameter.choices)}"

    @staticmethod
    def _http_action_line(action: HttpAction) -> str:
        if not action.parameters:
            return f"- {action.name}（无参数）"
        summaries = "；".join(
            GovernorRuntime._http_parameter_summary(parameter) for parameter in action.parameters
        )
        return f"- {action.name}（参数：{summaries}）"

    def http_task(
        self,
        action_name: str,
        params: dict[str, Any] | None = None,
        ack: str | None = None,
    ) -> dict[str, Any]:
        env, project = self._selected_project(action="running an http action", require_idle=True)
        name = action_name.strip()
        action = next((item for item in project.http_actions if item.name == name), None)
        if action is None:
            raise PermissionError("http action is not registered for this project")
        arguments = dict(params or {})
        # 对外发起的受控请求必须可追溯：记录谁、在哪个会话、带什么参数触发了哪个动作。
        logger.info(
            "governed http action requested: action=%s params=%s project=%s user=%s chat=%s",
            action.name,
            arguments,
            project.project_id,
            env.identity.user_id,
            env.identity.chat_id,
        )
        self._notify_once(
            env,
            "http",
            self._ack_or(ack, f"正在为 {project.display_name}执行“{action.name}”，请稍候。"),
        )
        result = self._http.run(action, arguments)
        # status 为 0 表示连不上目标；4xx/5xx 也不能报成功，否则模型会告诉用户已生效
        # （灯没插电时网关就回 409）。urllib 已跟随过重定向，剩下的 3xx 同样不算成功。
        succeeded = 200 <= result.status < 300
        logger.info(
            "governed http action finished: action=%s user=%s http_status=%s",
            action.name,
            env.identity.user_id,
            result.status,
        )
        return {
            "action": action.name,
            "status": "completed" if succeeded else "failed",
            "http_status": result.status,
            "body": result.body,
            "error": result.error,
        }

    def _selected_project_for_job(self) -> Project:
        _, project = self._selected_project(action="starting a local job", require_idle=True)
        return project

    def deliver_file(self, requested_path: str) -> dict[str, str | int | None]:
        env = self._require_authorized_environment()
        self._refresh_projects()
        record = self._record(env)
        if record.project_id is None:
            raise RuntimeError("select an authorized project before delivering a file")
        if not env.message_id:
            raise RuntimeError("file delivery requires the current WeCom message id")
        if self._delivery is None:
            raise RuntimeError("file delivery is not configured")
        delivery = self._delivery.prepare(
            self._projects[record.project_id],
            requested_path,
            env.message_id,
        )
        self._pending_deliveries[delivery.message_id].append(delivery)
        return {
            "channel": delivery.channel,
            "filename": delivery.filename,
            "size_bytes": delivery.size_bytes,
            "download_url": delivery.download_url,
            "status": "queued_for_current_reply",
        }

    def take_pending_delivery(self, message_id: str) -> ArtifactDelivery | None:
        pending = self._pending_deliveries.get(message_id)
        if not pending:
            return None
        delivery = pending.popleft()
        if not pending:
            self._pending_deliveries.pop(message_id, None)
        return delivery

    @staticmethod
    def _shares_session_scope(recorded: Identity, current: Identity) -> bool:
        # 群会话按 chatid 共享：同一会话范围（chat_id + chat_type 相同）内的
        # 授权成员共用一份记录；私聊 chat_id 即 userid，天然仍是一人一份。
        # 跨会话的记录绝不复用。
        return recorded.chat_id == current.chat_id and recorded.chat_type == current.chat_type

    def _record(self, env: SessionEnvironment) -> SessionRecord:
        record = self._state.load(env.session_key)
        if record is None or not self._shares_session_scope(record.identity, env.identity):
            return SessionRecord(env.identity)
        if record.project_id not in self._current_policy().authorized_project_ids(env.identity):
            return SessionRecord(env.identity)
        return record

    def _require_authorized_environment(self) -> SessionEnvironment:
        env = self._env_provider()
        if env.platform != "wecom" or not env.session_key:
            raise PermissionError("governor tools require an active WeCom session")
        if not self._identity_is_authorized(env.identity):
            raise PermissionError("session identity is not authorized")
        return env

    def _authorized_projects(self, identity: Identity) -> tuple[Project, ...]:
        policy = self._current_policy()
        ids = set(policy.authorized_project_ids(identity))
        return tuple(project for project in policy.projects if project.project_id in ids)

    def _resolve_authorized_project(self, identity: Identity, value: str) -> Project:
        normalized = value.strip()
        for project in self._authorized_projects(identity):
            if normalized in {project.project_id, project.display_name}:
                return project
        raise PermissionError(f"project {value!r} is not authorized")

    def _refresh_projects(self) -> None:
        if not self.config.project_discovery.enabled:
            return
        projects = build_project_catalog(
            tuple(
                project for project in self._explicit_projects if (project.path / ".git").exists()
            ),
            self.config.policy.permission_groups,
            self.config.project_discovery,
            runtime_root=self.config.runtime_root,
        )
        self._projects.clear()
        self._projects.update((project.project_id, project) for project in projects)

    def _current_policy(self) -> Policy:
        return Policy(tuple(self._projects.values()), self.config.policy.permission_groups)

    def _identity_is_authorized(self, identity: Identity) -> bool:
        return self.config.policy.matches_identity(identity)

    @staticmethod
    def _platform_name(platform: object) -> str:
        value = getattr(platform, "value", platform)
        return str(value or "").lower()
