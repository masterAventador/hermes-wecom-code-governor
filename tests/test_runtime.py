from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_wecom_code_governor.codex_runtime import (
    CodexMode,
    CodexRunResult,
    CodexTaskState,
)
from hermes_wecom_code_governor.config import (
    GovernorConfig,
    ProjectDiscoveryConfig,
    SafetyConfig,
)
from hermes_wecom_code_governor.delivery import ArtifactDelivery
from hermes_wecom_code_governor.discovery import discover_git_repositories
from hermes_wecom_code_governor.policy import (
    Identity,
    PermissionGroup,
    Policy,
    Project,
    RemoteAction,
)
from hermes_wecom_code_governor.project_job import ProjectJobResult
from hermes_wecom_code_governor.remote import RemoteRunResult
from hermes_wecom_code_governor.runtime import GovernorRuntime, SessionEnvironment, build_task_id
from hermes_wecom_code_governor.worktree import (
    ActiveWorktree,
    CompletionResult,
    CompletionStatus,
)


class MemoryState:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def set(self, key: str, value: object) -> None:
        self.values[key] = value


@dataclass
class FakeWorktrees:
    active: ActiveWorktree | None = None
    completed: bool = False
    reseeded: int = 0

    def ensure_seeded(self, active: ActiveWorktree) -> None:
        assert active == self.active
        self.reseeded += 1

    def begin(self, project: Project, task_id: str) -> ActiveWorktree:
        self.active = ActiveWorktree(
            project=project,
            task_id=task_id,
            path=Path("/runtime") / project.project_id / task_id,
            branch_name=f"bot/{task_id}",
            base_branch="dev",
            base_commit="a" * 40,
        )
        return self.active

    def complete(self, active: ActiveWorktree, limits: object) -> CompletionResult:
        assert active == self.active
        self.completed = True
        return CompletionResult(CompletionStatus.MERGED, commit="1234567")

    def codex_roots(self, active: ActiveWorktree) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        assert active == self.active
        return (
            (active.path, active.project.path / ".git"),
            (active.path, active.project.path / ".git" / "worktrees" / "task"),
        )


@dataclass
class FakeCodex:
    results: list[CodexRunResult]
    requests: list[object]

    def run(self, request: object) -> CodexRunResult:
        self.requests.append(request)
        return self.results.pop(0)


@dataclass
class FakeDelivery:
    result: ArtifactDelivery

    def __post_init__(self) -> None:
        self.calls: list[tuple[Project, str, str]] = []

    def prepare(self, project: Project, path: str, message_id: str) -> ArtifactDelivery:
        self.calls.append((project, path, message_id))
        return self.result

    def prepare_staged(
        self,
        path: Path,
        staging_root: Path,
        message_id: str,
    ) -> ArtifactDelivery:
        self.calls.append((path, staging_root, message_id))
        return self.result


@dataclass
class FakeJobs:
    result: ProjectJobResult

    def __post_init__(self) -> None:
        self.calls: list[tuple[Project, dict[str, object]]] = []

    def run(self, project: Project, **kwargs: object) -> ProjectJobResult:
        self.calls.append((project, kwargs))
        return self.result


@dataclass
class FakeRemote:
    stdout: str = "VPP-AAAA-BBBB-CCCC"
    exit_code: int = 0
    stderr: str = ""

    def __post_init__(self) -> None:
        self.calls: list[RemoteAction] = []

    def run(self, action: RemoteAction) -> RemoteRunResult:
        self.calls.append(action)
        return RemoteRunResult(self.exit_code, self.stdout, self.stderr)


class FakeInspector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Project, dict]] = []

    def files(self, project: Project, **kwargs: object) -> dict:
        self.calls.append(("files", project, kwargs))
        return {"files": [{"path": "release/app.exe"}]}

    def read(self, project: Project, **kwargs: object) -> dict:
        self.calls.append(("read", project, kwargs))
        return {"files": [{"path": "README.md", "content": "Demo"}]}

    def search(self, project: Project, **kwargs: object) -> dict:
        self.calls.append(("search", project, kwargs))
        return {"matches": []}

    def git(self, project: Project, **kwargs: object) -> dict:
        self.calls.append(("git", project, kwargs))
        return {"output": "clean"}


def make_runtime(
    *,
    state: MemoryState | None = None,
    env: SessionEnvironment | None = None,
    worktrees: FakeWorktrees | None = None,
    codex: FakeCodex | None = None,
    delivery: FakeDelivery | None = None,
    jobs: FakeJobs | None = None,
    inspector: FakeInspector | None = None,
    notifier: object | None = None,
    projects: tuple[Project, ...] | None = None,
    remote: FakeRemote | None = None,
) -> GovernorRuntime:
    configured_projects = projects or (
        Project("aijd-demo", "AIJD测试项目", Path("/Users/aventador/sourceCode/bjx/aijd-demo")),
        Project(
            "vpp-digital-twin",
            "VPP数字孪生项目",
            Path("/Users/aventador/sourceCode/vpp-digital-twin"),
        ),
    )
    policy = Policy(
        configured_projects,
        (
            PermissionGroup(
                "owner",
                frozenset({"user-1"}),
                frozenset({"chat-1", "user-1"}),
                frozenset(project.project_id for project in configured_projects),
            ),
        ),
    )
    config = GovernorConfig(Path("/runtime"), SafetyConfig(), policy)
    current = env or SessionEnvironment(
        platform="wecom",
        session_key="agent:main:wecom:group:chat-1:user-1",
        identity=Identity("user-1", "chat-1", "group"),
        message_id="message-1",
    )
    return GovernorRuntime(
        config,
        state or MemoryState(),
        env_provider=lambda: current,
        worktrees=worktrees or FakeWorktrees(),
        codex=codex or FakeCodex([], []),
        delivery=delivery,
        jobs=jobs,
        inspector=inspector,
        notifier=notifier,
        remote=remote,
        now=lambda: (8, 17),
    )


def make_discovery_runtime(
    tmp_path: Path,
    *,
    with_repository: bool = True,
    auto_discovered: bool = True,
) -> tuple[GovernorRuntime, Path, Project | None]:
    source_root = tmp_path / "sourceCode"
    source_root.mkdir()
    initial_project = None
    if with_repository:
        initial_path = source_root / "initial-service"
        initial_path.mkdir()
        (initial_path / ".git").mkdir()
        if auto_discovered:
            discovered = discover_git_repositories((source_root,))[0]
            initial_project = Project(
                discovered.project_id,
                discovered.display_name,
                discovered.path,
                auto_discovered=True,
            )
        else:
            initial_project = Project("initial-service", "初始服务", initial_path)
    projects = (initial_project,) if initial_project is not None else ()
    permission = PermissionGroup(
        "owner",
        frozenset({"user-1"}),
        frozenset({"*"}),
        root_paths=(source_root,),
    )
    config = GovernorConfig(
        tmp_path / "runtime",
        SafetyConfig(),
        Policy(projects, (permission,)),
        project_discovery=ProjectDiscoveryConfig(enabled=True, max_projects=20),
    )
    env = SessionEnvironment(
        platform="wecom",
        session_key="agent:main:wecom:group:chat-1:user-1",
        identity=Identity("user-1", "chat-1", "group"),
        message_id="message-1",
    )
    runtime = GovernorRuntime(
        config,
        MemoryState(),
        env_provider=lambda: env,
        worktrees=FakeWorktrees(),
        codex=FakeCodex([], []),
        inspector=FakeInspector(),
        now=lambda: (8, 18),
    )
    return runtime, source_root, initial_project


def event(user: str, chat: str, platform: str = "wecom") -> object:
    return SimpleNamespace(
        source=SimpleNamespace(
            platform=SimpleNamespace(value=platform),
            user_id=user,
            chat_id=chat,
            chat_type="group",
        )
    )


def test_unauthorized_wecom_message_is_silently_skipped_before_model() -> None:
    runtime = make_runtime()

    assert runtime.pre_gateway_dispatch(event("stranger", "chat-1")) == {
        "action": "skip",
        "reason": "not authorized by code governor",
    }
    assert runtime.pre_gateway_dispatch(event("user-1", "chat-1")) == {"action": "allow"}
    assert runtime.pre_gateway_dispatch(event("stranger", "chat-1", "telegram")) == {
        "action": "allow"
    }


def test_authorized_wecom_event_carries_trigger_message_id_into_session_context() -> None:
    runtime = make_runtime()
    incoming = event("user-1", "chat-1")
    incoming.message_id = "wecom-message-123"
    incoming.source.message_id = None

    assert runtime.pre_gateway_dispatch(incoming) == {"action": "allow"}
    assert incoming.source.message_id == "wecom-message-123"


def test_prompt_context_describes_identity_flexible_project_choice_and_current_state() -> None:
    runtime = make_runtime()

    context = runtime.pre_llm_call()["context"]

    assert "不要透露真实模型" in context
    assert "你的简短身份介绍固定为" not in context
    assert "存在的意义" not in context
    assert "GPT" not in context
    assert "授权项目：" not in context
    assert "AIJD测试项目" not in context
    assert "VPP数字孪生项目" not in context
    assert "自动发现" not in context
    assert "你服务的是已授权用户" not in context
    assert "先判断当前请求是否需要访问具体项目" in context
    assert "不要使用关键词路由" in context
    assert "当前尚未选择项目" in context
    assert "打包、上传或部署" in context and "明确要求" in context
    assert "governor_project_files" in context
    assert "governor_project_read" in context
    assert "governor_project_search" in context
    assert "governor_project_git" in context
    assert "governor_codex_read" not in context
    assert "governor_codex_change" in context
    assert "governor_deliver_file" in context
    assert "governor_remote_task" in context
    assert "governor_project_job" in context
    assert "只有修改代码才调用 governor_codex_change" in context
    assert "先用一行引用用户这次的原始需求原话" in context
    assert "打包、测试、导出" in context
    assert "不要自行使用外层原生文件或终端工具" not in context
    assert "argv 数组" not in context
    assert "50MiB" not in context
    assert "只回答用户明确提出的问题" in context
    assert "可用工具只有以 governor_ 开头的治理工具和基础会话工具" in context
    assert "不要尝试调用任何其他工具" in context
    assert "任务状态、退出码、基准提交、文件大小" in context
    assert "一句直接结果和一个下载入口" in context
    assert "不要复述工具返回的下载地址" in context


def test_runtime_project_catalog_refreshes_after_repository_addition_and_removal(
    tmp_path: Path,
) -> None:
    runtime, source_root, initial_project = make_discovery_runtime(tmp_path)
    assert initial_project is not None

    runtime.select_project(initial_project.project_id)
    added_path = source_root / "new-service"
    added_path.mkdir()
    (added_path / ".git").mkdir()

    added = runtime.list_projects(query="new-service")

    assert added["total"] == 1
    assert added["projects"][0]["path"] == str(added_path)

    (initial_project.path / ".git").rmdir()

    remaining = runtime.list_projects()

    assert {project["path"] for project in remaining["projects"]} == {str(added_path)}
    with pytest.raises(RuntimeError, match="select"):
        runtime.project_git(action="status")


def test_permission_group_authorizes_conversation_when_root_has_no_projects(
    tmp_path: Path,
) -> None:
    runtime, _, _ = make_discovery_runtime(tmp_path, with_repository=False)

    assert runtime.is_authorized_identity(Identity("user-1", "chat-1", "group"))
    assert runtime.pre_gateway_dispatch(event("user-1", "chat-1")) == {"action": "allow"}
    assert runtime.pre_llm_call() is not None
    assert not runtime.is_authorized_identity(Identity("stranger", "chat-1", "group"))


def test_runtime_project_catalog_removes_deleted_explicit_repository(tmp_path: Path) -> None:
    runtime, _, project = make_discovery_runtime(tmp_path, auto_discovered=False)
    assert project is not None

    runtime.select_project(project.project_id)
    (project.path / ".git").rmdir()

    context = runtime.pre_llm_call()["context"]
    result = runtime.list_projects()

    assert "当前尚未选择项目" in context
    assert "当前项目：初始服务" not in context
    assert result["projects"] == []
    record = runtime.session_record_for_adapter("agent:main:wecom:group:chat-1:user-1")
    assert record is not None
    assert record.project_id is None


def test_selected_project_persists_and_can_be_switched_explicitly() -> None:
    state = MemoryState()
    first = make_runtime(state=state)
    selected = first.select_project("VPP数字孪生项目")
    second = make_runtime(state=state)

    assert selected["project_id"] == "vpp-digital-twin"
    assert "当前项目：VPP数字孪生项目" in second.pre_llm_call()["context"]
    switched = second.select_project("aijd-demo")
    assert switched["display_name"] == "AIJD测试项目"


def test_adapter_can_resolve_authorized_projects_from_persisted_session_identity() -> None:
    runtime = make_runtime()
    session_key = "agent:main:wecom:group:chat-1:user-1"

    runtime.pre_llm_call()

    assert runtime.project_choices_for_session(session_key) == {
        "aijd-demo": "AIJD测试项目",
        "vpp-digital-twin": "VPP数字孪生项目",
    }
    assert runtime.session_record_for_adapter(session_key).identity == Identity(
        "user-1", "chat-1", "group"
    )
    selected = runtime.select_project_for_session(
        session_key,
        Identity("user-1", "chat-1", "group"),
        "vpp-digital-twin",
    )
    assert selected["display_name"] == "VPP数字孪生项目"
    with pytest.raises(PermissionError):
        runtime.select_project_for_session(
            session_key,
            Identity("other-user", "chat-1", "group"),
            "aijd-demo",
        )


def test_project_listing_can_search_and_limit_a_large_authorized_catalog() -> None:
    runtime = make_runtime()

    result = runtime.list_projects(query="vpp", limit=1)

    assert result == {
        "projects": [
            {
                "project_id": "vpp-digital-twin",
                "display_name": "VPP数字孪生项目",
                "path": "/Users/aventador/sourceCode/vpp-digital-twin",
            }
        ],
        "total": 1,
        "truncated": False,
        "match_kind": "contains",
    }


def test_project_listing_prefers_an_exact_project_over_nested_prefix_matches() -> None:
    outer = Project(
        "auto-automation-tool",
        "automation-tool",
        Path("/workspace/sourceCode/automation-tool"),
        auto_discovered=True,
    )
    nested = Project(
        "auto-hyperframes",
        "automation-tool/vendor/hyperframes",
        Path("/workspace/sourceCode/automation-tool/vendor/hyperframes"),
        auto_discovered=True,
    )
    runtime = make_runtime(projects=(outer, nested))

    result = runtime.list_projects(query="automation-tool")

    assert result == {
        "projects": [
            {
                "project_id": "auto-automation-tool",
                "display_name": "automation-tool",
                "path": "/workspace/sourceCode/automation-tool",
            }
        ],
        "total": 1,
        "truncated": False,
        "match_kind": "exact",
    }


def test_outer_hermes_file_and_terminal_tools_are_always_blocked() -> None:
    runtime = make_runtime()

    assert runtime.pre_tool_call("read_file", {"path": "README.md"})["action"] == "block"
    runtime.select_project("aijd-demo")
    for tool_name in ("read_file", "write_file", "patch", "search_files", "terminal"):
        directive = runtime.pre_tool_call(tool_name, {"path": "README.md"})
        assert directive["action"] == "block"
        assert "code governor" in directive["message"]


def test_local_side_effect_tools_outside_governed_surface_are_blocked() -> None:
    runtime = make_runtime()

    for tool_name in ("execute_code", "computer_use", "project_create", "browser_navigate"):
        directive = runtime.pre_tool_call(tool_name, {})
        assert directive["action"] == "block"
        assert "not enabled" in directive["message"]
    assert runtime.pre_tool_call("clarify", {"question": "请补充信息"}) is None
    assert runtime.pre_tool_call("web_search", {"query": "FastAPI"}) is None


def test_begin_and_complete_task_persist_active_worktree_and_merge_result() -> None:
    state = MemoryState()
    worktrees = FakeWorktrees()
    runtime = make_runtime(state=state, worktrees=worktrees)
    runtime.select_project("aijd-demo")

    started = runtime.begin_task("修改说明标题")
    assert started["task_id"] == "0817-修改说明标题"
    assert started["worktree_path"].endswith("/aijd-demo/0817-修改说明标题")
    assert "当前有活动 worktree" in runtime.pre_llm_call()["context"]

    result = runtime.complete_task()
    assert result == {"status": "merged", "commit": "1234567", "message": ""}
    assert worktrees.completed
    assert "当前有活动 worktree" not in runtime.pre_llm_call()["context"]


def test_project_reads_use_outer_governed_tools_without_starting_codex() -> None:
    inspector = FakeInspector()
    codex = FakeCodex([], [])
    runtime = make_runtime(codex=codex, inspector=inspector)
    runtime.select_project("aijd-demo")

    files = runtime.project_files(path="release", pattern="*.exe", sha256=True)
    read = runtime.project_read(paths=["README.md"])
    search = runtime.project_search(query="FastAPI")
    git = runtime.project_git(action="status")

    assert files["project"] == "AIJD测试项目"
    assert read["files"][0]["content"] == "Demo"
    assert search["matches"] == []
    assert git["output"] == "clean"
    assert [call[0] for call in inspector.calls] == ["files", "read", "search", "git"]
    assert codex.requests == []


def test_project_job_runs_without_inner_codex_and_queues_generated_artifact(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "runtime" / "artifacts" / "job" / "app.exe"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"artifact")
    jobs = FakeJobs(
        ProjectJobResult(
            status="completed",
            exit_code=0,
            output="build completed",
            base_commit="a" * 40,
            artifacts=(artifact,),
            staging_root=artifact.parent,
        )
    )
    delivery_result = ArtifactDelivery(
        message_id="message-1",
        channel="cos",
        path=artifact,
        filename="app.exe",
        size_bytes=8,
        download_url="https://cos.example/app.exe",
    )
    delivery = FakeDelivery(delivery_result)
    codex = FakeCodex([], [])
    notices: list[str] = []
    runtime = make_runtime(
        jobs=jobs,
        codex=codex,
        delivery=delivery,
        notifier=lambda _identity, text: notices.append(text),
    )
    runtime.select_project("vpp-digital-twin")

    result = runtime.project_job(
        argv=["npm", "run", "build:win"],
        artifact_globs=["release/*.exe"],
        title="生成安装包",
    )

    assert result["status"] == "completed"
    assert result["task_id"] == "0817-生成安装包"
    assert result["output"] == "build completed"
    assert result["artifacts"] == [
        {
            "channel": "cos",
            "filename": "app.exe",
            "size_bytes": 8,
            "download_url": "https://cos.example/app.exe",
        }
    ]
    assert codex.requests == []
    assert notices == ["正在为 VPP数字孪生项目执行“生成安装包”，请稍候。"]
    job_project, job_args = jobs.calls[0]
    assert job_project.project_id == "vpp-digital-twin"
    assert str(job_args["job_id"]).startswith("0817-生成安装包--")
    assert job_args["argv"] == ("npm", "run", "build:win")
    assert job_args["artifact_globs"] == ("release/*.exe",)
    staged_path, staging_root, message_id = delivery.calls[0]
    assert staged_path == artifact
    assert staging_root == artifact.parent
    assert message_id == "message-1"
    assert runtime.take_pending_delivery("message-1") == delivery_result


def _remote_project() -> Project:
    return Project(
        "vpp-digital-twin",
        "VPP数字孪生项目",
        Path("/Users/aventador/sourceCode/vpp-digital-twin"),
        remote_actions=(
            RemoteAction(
                name="生成激活码",
                host="root@license.example",
                argv=("node", "/opt/vpp-license/issue.mjs"),
                timeout_seconds=45,
            ),
        ),
    )


def test_prompt_context_lists_registered_remote_action_names_after_selection() -> None:
    runtime = make_runtime(projects=(_remote_project(),))
    runtime.select_project("vpp-digital-twin")

    context = runtime.pre_llm_call()["context"]

    assert "当前项目可触发的远程受控动作" in context
    assert "- 生成激活码" in context


def test_remote_task_runs_named_action_and_returns_output() -> None:
    remote = FakeRemote(stdout="VPP-5QH2-34MZ-HRRU\n")
    notices: list[str] = []
    runtime = make_runtime(
        projects=(_remote_project(),),
        remote=remote,
        notifier=lambda _identity, text: notices.append(text),
    )
    runtime.select_project("vpp-digital-twin")

    result = runtime.remote_task("生成激活码")

    assert result["status"] == "completed"
    assert result["action"] == "生成激活码"
    assert result["output"] == "VPP-5QH2-34MZ-HRRU"
    assert [action.name for action in remote.calls] == ["生成激活码"]
    assert notices == ["正在为 VPP数字孪生项目执行“生成激活码”，请稍候。"]


def test_remote_task_rejects_an_unregistered_action_without_running() -> None:
    remote = FakeRemote()
    runtime = make_runtime(projects=(_remote_project(),), remote=remote)
    runtime.select_project("vpp-digital-twin")

    with pytest.raises(PermissionError, match="remote action is not registered"):
        runtime.remote_task("删除数据库")

    assert remote.calls == []


def test_remote_task_reports_failure_output_when_command_exits_nonzero() -> None:
    remote = FakeRemote(exit_code=255, stdout="", stderr="ssh: connect timed out")
    runtime = make_runtime(projects=(_remote_project(),), remote=remote)
    runtime.select_project("vpp-digital-twin")

    result = runtime.remote_task("生成激活码")

    assert result["status"] == "failed"
    assert result["exit_code"] == 255
    assert "connect timed out" in result["output"]


def test_remote_task_success_output_ignores_ssh_stderr_warnings() -> None:
    remote = FakeRemote(
        exit_code=0,
        stdout="VPP-CODE-1234\n",
        stderr="Warning: Permanently added host to known hosts.",
    )
    runtime = make_runtime(projects=(_remote_project(),), remote=remote)
    runtime.select_project("vpp-digital-twin")

    result = runtime.remote_task("生成激活码")

    assert result["output"] == "VPP-CODE-1234"


def test_remote_task_truncates_flooding_output() -> None:
    remote = FakeRemote(exit_code=1, stdout="", stderr="e" * 20_000)
    runtime = make_runtime(projects=(_remote_project(),), remote=remote)
    runtime.select_project("vpp-digital-twin")

    result = runtime.remote_task("生成激活码")

    assert len(str(result["output"])) < 13_000
    assert "个字符已省略" in str(result["output"])


def test_remote_task_is_blocked_while_a_code_task_is_active() -> None:
    remote = FakeRemote()
    worktrees = FakeWorktrees()
    state = MemoryState()
    codex = FakeCodex([CodexRunResult("write-thread", "请确认。", CodexTaskState.NEEDS_INPUT)], [])
    runtime = make_runtime(
        state=state,
        projects=(_remote_project(),),
        remote=remote,
        worktrees=worktrees,
        codex=codex,
    )
    runtime.select_project("vpp-digital-twin")
    runtime.codex_change("改点东西", "改代码")

    with pytest.raises(RuntimeError, match="finish the active code task"):
        runtime.remote_task("生成激活码")

    assert remote.calls == []


def test_remote_task_writes_an_audit_line_with_identity(caplog: pytest.LogCaptureFixture) -> None:
    remote = FakeRemote(stdout="VPP-CODE-9999\n")
    runtime = make_runtime(projects=(_remote_project(),), remote=remote)
    runtime.select_project("vpp-digital-twin")

    with caplog.at_level("INFO", logger="hermes_wecom_code_governor.runtime"):
        runtime.remote_task("生成激活码")

    audit = [record for record in caplog.records if "remote action" in record.getMessage()]
    assert audit
    message = audit[0].getMessage()
    assert "生成激活码" in message
    assert "user-1" in message


def test_artifact_job_fails_before_execution_when_delivery_is_not_configured(
    tmp_path: Path,
) -> None:
    jobs = FakeJobs(ProjectJobResult("completed", 0, "ok", "a" * 40))
    runtime = make_runtime(jobs=jobs)
    runtime.select_project("vpp-digital-twin")

    with pytest.raises(RuntimeError, match="delivery"):
        runtime.project_job(
            argv=["npm", "run", "build:win"],
            artifact_globs=["release/*.exe"],
            title="生成安装包",
        )

    assert jobs.calls == []


def test_project_read_tools_require_a_selected_authorized_project() -> None:
    runtime = make_runtime(inspector=FakeInspector())

    with pytest.raises(RuntimeError, match="select"):
        runtime.project_read(paths=["README.md"])


def test_codex_change_sends_a_single_brief_notice_before_starting() -> None:
    notices: list[tuple[Identity, str]] = []
    worktrees = FakeWorktrees()

    class NoticeAwareCodex:
        def run(self, request: object) -> CodexRunResult:
            assert notices == [
                (
                    Identity("user-1", "chat-1", "group"),
                    "正在修改 AIJD测试项目，请稍候。",
                )
            ]
            return CodexRunResult("write-thread", "已经修改", CodexTaskState.COMPLETED)

    runtime = make_runtime(
        worktrees=worktrees,
        codex=NoticeAwareCodex(),
        notifier=lambda identity, text: notices.append((identity, text)),
    )
    runtime.select_project("aijd-demo")

    result = runtime.codex_change("修改标题", "修改标题")

    assert result["status"] == "merged"


def test_completed_codex_change_is_validated_merged_and_cleared() -> None:
    state = MemoryState()
    worktrees = FakeWorktrees()
    codex = FakeCodex(
        [
            CodexRunResult(
                "write-thread",
                "已经修复登录问题。",
                CodexTaskState.COMPLETED,
            )
        ],
        [],
    )
    runtime = make_runtime(state=state, worktrees=worktrees, codex=codex)
    runtime.select_project("aijd-demo")

    result = runtime.codex_change("修复登录问题", "修复登录")

    assert result == {
        "answer": "已经修复登录问题。",
        "status": "merged",
        "project": "AIJD测试项目",
        "commit": "1234567",
        "message": "",
    }
    request = codex.requests[0]
    assert request.mode is CodexMode.WRITE
    assert request.thread_id is None
    assert request.cwd == worktrees.active.path
    assert worktrees.completed
    assert "当前有活动 worktree" not in runtime.pre_llm_call()["context"]


def test_codex_change_needing_input_keeps_worktree_and_resumes_same_thread() -> None:
    state = MemoryState()
    worktrees = FakeWorktrees()
    codex = FakeCodex(
        [
            CodexRunResult(
                "write-thread",
                "请确认应该修改哪个接口。",
                CodexTaskState.NEEDS_INPUT,
            ),
            CodexRunResult(
                "write-thread",
                "已经按确认结果修改。",
                CodexTaskState.COMPLETED,
            ),
        ],
        [],
    )
    runtime = make_runtime(state=state, worktrees=worktrees, codex=codex)
    runtime.select_project("aijd-demo")

    waiting = runtime.codex_change("修复接口问题", "修复接口")
    completed = runtime.codex_change("修改登录接口", "不会创建新任务")

    assert waiting["status"] == "needs_input"
    assert waiting["answer"] == "请确认应该修改哪个接口。"
    assert not worktrees.completed or completed["status"] == "merged"
    assert codex.requests[1].thread_id == "write-thread"
    assert codex.requests[1].cwd == codex.requests[0].cwd
    assert completed["status"] == "merged"


def test_same_message_retry_sends_the_brief_notice_only_once() -> None:
    notices: list[str] = []
    state = MemoryState()
    worktrees = FakeWorktrees()
    codex = FakeCodex(
        [
            CodexRunResult("write-thread", "请确认修改范围。", CodexTaskState.NEEDS_INPUT),
            CodexRunResult("write-thread", "请再确认一次。", CodexTaskState.NEEDS_INPUT),
        ],
        [],
    )
    runtime = make_runtime(
        state=state,
        worktrees=worktrees,
        codex=codex,
        notifier=lambda _identity, text: notices.append(text),
    )
    runtime.select_project("aijd-demo")

    runtime.codex_change("修复接口问题", "修复接口")
    runtime.codex_change("修复接口问题", "修复接口")

    assert len(notices) == 1


def test_new_message_sends_the_brief_notice_again() -> None:
    notices: list[str] = []
    state = MemoryState()
    worktrees = FakeWorktrees()
    codex = FakeCodex(
        [
            CodexRunResult("write-thread", "请确认修改范围。", CodexTaskState.NEEDS_INPUT),
            CodexRunResult("write-thread", "已经修改。", CodexTaskState.COMPLETED),
        ],
        [],
    )
    environments = iter(
        [
            SessionEnvironment(
                platform="wecom",
                session_key="agent:main:wecom:group:chat-1:user-1",
                identity=Identity("user-1", "chat-1", "group"),
                message_id="message-1",
            ),
            SessionEnvironment(
                platform="wecom",
                session_key="agent:main:wecom:group:chat-1:user-1",
                identity=Identity("user-1", "chat-1", "group"),
                message_id="message-2",
            ),
        ]
    )
    current = next(environments)

    def env_provider() -> SessionEnvironment:
        return current

    runtime = GovernorRuntime(
        GovernorConfig(
            Path("/runtime"),
            SafetyConfig(),
            Policy(
                (
                    Project(
                        "aijd-demo",
                        "AIJD测试项目",
                        Path("/Users/aventador/sourceCode/bjx/aijd-demo"),
                    ),
                ),
                (
                    PermissionGroup(
                        "owner",
                        frozenset({"user-1"}),
                        frozenset({"chat-1"}),
                        frozenset({"aijd-demo"}),
                    ),
                ),
            ),
        ),
        state,
        env_provider=env_provider,
        worktrees=worktrees,
        codex=codex,
        notifier=lambda _identity, text: notices.append(text),
        now=lambda: (8, 18),
    )
    runtime.select_project("aijd-demo")
    runtime.codex_change("修复接口问题", "修复接口")
    current = next(environments)
    runtime.codex_change("继续完成修改", "不会创建新任务")

    assert len(notices) == 2


def test_codex_change_grants_project_readable_paths_to_codex() -> None:
    worktrees = FakeWorktrees()
    codex = FakeCodex(
        [CodexRunResult("write-thread", "已经修改。", CodexTaskState.COMPLETED)],
        [],
    )
    runtime = make_runtime(
        worktrees=worktrees,
        codex=codex,
        projects=(
            Project(
                "vpp-digital-twin",
                "VPP数字孪生项目",
                Path("/Users/aventador/sourceCode/vpp-digital-twin"),
                readable_paths=(Path("/opt/homebrew"), Path("/System/Cryptexes")),
            ),
        ),
    )
    runtime.select_project("vpp-digital-twin")

    runtime.codex_change("修改模式高亮", "模式高亮")

    request = codex.requests[0]
    worktree_readable, _ = worktrees.codex_roots(worktrees.active)
    assert request.readable_roots == (
        *worktree_readable,
        Path("/opt/homebrew"),
        Path("/System/Cryptexes"),
    )


def test_codex_change_reseeds_a_resumed_worktree_before_starting_codex() -> None:
    state = MemoryState()
    worktrees = FakeWorktrees()
    codex = FakeCodex(
        [
            CodexRunResult("write-thread", "请确认修改范围。", CodexTaskState.NEEDS_INPUT),
            CodexRunResult("write-thread", "已经修改。", CodexTaskState.COMPLETED),
        ],
        [],
    )
    runtime = make_runtime(state=state, worktrees=worktrees, codex=codex)
    runtime.select_project("aijd-demo")

    runtime.codex_change("修复接口问题", "修复接口")
    assert worktrees.reseeded == 0

    runtime.codex_change("继续完成修改", "不会创建新任务")
    assert worktrees.reseeded == 1


def make_shared_chat_runtime(state: MemoryState, identity: Identity) -> GovernorRuntime:
    env = SessionEnvironment(
        platform="wecom",
        session_key="agent:main:wecom:group:chat-1",
        identity=identity,
        message_id=f"message-{identity.user_id}",
    )
    return GovernorRuntime(
        GovernorConfig(
            Path("/runtime"),
            SafetyConfig(),
            Policy(
                (
                    Project(
                        "aijd-demo",
                        "AIJD测试项目",
                        Path("/Users/aventador/sourceCode/bjx/aijd-demo"),
                    ),
                ),
                (
                    PermissionGroup(
                        "team",
                        frozenset({"user-1", "user-3"}),
                        frozenset({"*"}),
                        frozenset({"aijd-demo"}),
                    ),
                ),
            ),
        ),
        state,
        env_provider=lambda: env,
        worktrees=FakeWorktrees(),
        codex=FakeCodex([], []),
        now=lambda: (8, 19),
    )


def test_authorized_members_of_the_same_group_chat_share_session_state() -> None:
    state = MemoryState()
    first = make_shared_chat_runtime(state, Identity("user-1", "chat-1", "group"))
    first.select_project("aijd-demo")

    second = make_shared_chat_runtime(state, Identity("user-3", "chat-1", "group"))
    context = second.pre_llm_call()["context"]

    assert "当前项目：AIJD测试项目" in context
    # 第二个人的消息不能把第一个人建立的会话状态冲掉
    again = make_shared_chat_runtime(state, Identity("user-1", "chat-1", "group"))
    assert "当前项目：AIJD测试项目" in again.pre_llm_call()["context"]


def test_record_from_a_different_chat_is_never_reused() -> None:
    state = MemoryState()
    first = make_shared_chat_runtime(state, Identity("user-1", "chat-1", "group"))
    first.select_project("aijd-demo")

    other_chat = make_shared_chat_runtime(state, Identity("user-1", "chat-2", "group"))
    assert "当前尚未选择项目" in other_chat.pre_llm_call()["context"]


def test_identity_mismatch_cannot_reuse_persisted_session_state() -> None:
    state = MemoryState()
    first = make_runtime(state=state)
    first.select_project("aijd-demo")
    mismatched = SessionEnvironment(
        platform="wecom",
        session_key="agent:main:wecom:group:chat-1:user-1",
        identity=Identity("user-2", "chat-1", "group"),
    )
    second = make_runtime(state=state, env=mismatched)

    with pytest.raises(PermissionError):
        second.select_project("aijd-demo")
    assert second.pre_tool_call("read_file", {"path": "README.md"})["action"] == "block"


def test_task_id_uses_month_day_and_semantic_title_without_random_suffix() -> None:
    assert build_task_id("  修改 README 标题！ ", month=8, day=17) == "0817-修改-README-标题"
    with pytest.raises(ValueError, match="title"):
        build_task_id("!!!", month=8, day=17)


def test_authorized_selected_project_can_queue_a_file_for_the_same_message() -> None:
    prepared = ArtifactDelivery(
        message_id="message-1",
        channel="wecom",
        path=Path("/Users/aventador/sourceCode/bjx/aijd-demo/release/app.zip"),
        filename="app.zip",
        size_bytes=1024,
        download_url=None,
    )
    delivery = FakeDelivery(prepared)
    runtime = make_runtime(delivery=delivery)
    runtime.select_project("aijd-demo")

    result = runtime.deliver_file("release/app.zip")

    assert result["channel"] == "wecom"
    assert result["filename"] == "app.zip"
    assert delivery.calls[0][1:] == ("release/app.zip", "message-1")
    assert runtime.take_pending_delivery("other-message") is None
    assert runtime.take_pending_delivery("message-1") == prepared
    assert runtime.take_pending_delivery("message-1") is None


def test_file_delivery_requires_a_selected_project_and_message_context() -> None:
    prepared = ArtifactDelivery(
        message_id="message-1",
        channel="wecom",
        path=Path("/tmp/app.zip"),
        filename="app.zip",
        size_bytes=1024,
        download_url=None,
    )
    runtime = make_runtime(delivery=FakeDelivery(prepared))

    with pytest.raises(RuntimeError, match="select"):
        runtime.deliver_file("release/app.zip")

    env_without_message = SessionEnvironment(
        platform="wecom",
        session_key="agent:main:wecom:group:chat-1:user-1",
        identity=Identity("user-1", "chat-1", "group"),
        message_id="",
    )
    second = make_runtime(env=env_without_message, delivery=FakeDelivery(prepared))
    second.select_project("aijd-demo")
    with pytest.raises(RuntimeError, match="message"):
        second.deliver_file("release/app.zip")
