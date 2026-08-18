import json
from pathlib import Path

from hermes_wecom_code_governor.plugin import register_runtime_components, resolve_config_path

ROOT = Path(__file__).resolve().parents[1]


class FakeContext:
    def __init__(self) -> None:
        self.hooks: dict[str, object] = {}
        self.tools: dict[str, dict] = {}

    def register_hook(self, name: str, callback: object) -> None:
        self.hooks[name] = callback

    def register_tool(self, **kwargs: object) -> None:
        self.tools[str(kwargs["name"])] = kwargs


class FakeRuntime:
    def pre_gateway_dispatch(self, **kwargs: object) -> dict:
        return {"action": "allow"}

    def pre_llm_call(self, **kwargs: object) -> dict:
        return {"context": "governed"}

    def pre_tool_call(self, **kwargs: object) -> None:
        return None

    def list_projects(self, *, query: str = "", limit: int = 20) -> dict:
        return {"projects": [], "query": query, "limit": limit}

    def select_project(self, value: str) -> dict:
        return {"project_id": value}

    def begin_task(self, title: str) -> dict:
        return {"task_id": title}

    def complete_task(self) -> dict:
        return {"status": "merged"}

    def project_files(self, **kwargs: object) -> dict:
        return {"files": [kwargs]}

    def project_read(self, **kwargs: object) -> dict:
        return {"read": kwargs}

    def project_search(self, **kwargs: object) -> dict:
        return {"search": kwargs}

    def project_git(self, **kwargs: object) -> dict:
        return {"git": kwargs}

    def codex_change(self, request: str, title: str) -> dict:
        return {"answer": request, "task_id": title}

    def project_job(
        self,
        *,
        argv: list[str],
        artifact_globs: list[str],
        title: str,
    ) -> dict:
        return {"argv": argv, "artifact_globs": artifact_globs, "title": title}

    def deliver_file(self, path: str) -> dict:
        return {"channel": "wecom", "filename": path}


def call_tool(entry: dict, args: dict) -> dict:
    return json.loads(entry["handler"](args))


def test_registers_governance_hooks_and_model_tools() -> None:
    ctx = FakeContext()
    runtime = FakeRuntime()

    register_runtime_components(ctx, runtime)

    assert set(ctx.hooks) == {"pre_gateway_dispatch", "pre_llm_call", "pre_tool_call"}
    assert set(ctx.tools) == {
        "governor_list_projects",
        "governor_select_project",
        "governor_project_files",
        "governor_project_read",
        "governor_project_search",
        "governor_project_git",
        "governor_codex_change",
        "governor_project_job",
        "governor_deliver_file",
    }
    assert call_tool(ctx.tools["governor_list_projects"], {"query": "vpp", "limit": 6}) == {
        "projects": [],
        "query": "vpp",
        "limit": 6,
    }
    assert call_tool(ctx.tools["governor_select_project"], {"project": "demo"}) == {
        "project_id": "demo"
    }
    assert call_tool(
        ctx.tools["governor_project_files"],
        {"path": "release", "pattern": "*.exe", "sha256": True},
    ) == {
        "files": [
            {
                "path": "release",
                "pattern": "*.exe",
                "recursive": False,
                "limit": 50,
                "sort": "path",
                "sha256": True,
            }
        ]
    }
    assert call_tool(ctx.tools["governor_project_read"], {"paths": ["README.md", "package.json"]})[
        "read"
    ]["paths"] == ["README.md", "package.json"]
    assert (
        call_tool(ctx.tools["governor_project_search"], {"query": "FastAPI"})["search"]["query"]
        == "FastAPI"
    )
    assert call_tool(ctx.tools["governor_project_git"], {"action": "status"}) == {
        "git": {"action": "status", "limit": 20, "revision": None}
    }
    assert call_tool(
        ctx.tools["governor_codex_change"],
        {"request": "修复登录", "title": "登录修复"},
    ) == {
        "answer": "修复登录",
        "task_id": "登录修复",
    }
    assert call_tool(
        ctx.tools["governor_project_job"],
        {
            "argv": ["npm", "run", "build:win"],
            "artifact_globs": ["release/*.exe"],
            "title": "生成安装包",
        },
    ) == {
        "argv": ["npm", "run", "build:win"],
        "artifact_globs": ["release/*.exe"],
        "title": "生成安装包",
    }
    assert call_tool(
        ctx.tools["governor_deliver_file"],
        {"path": "release/app.zip"},
    ) == {"channel": "wecom", "filename": "release/app.zip"}


def test_tool_handler_returns_a_model_visible_error_instead_of_raising() -> None:
    ctx = FakeContext()
    register_runtime_components(ctx, FakeRuntime())

    result = call_tool(ctx.tools["governor_select_project"], {})

    assert "error" in result
    assert "project" in result["error"]


def test_missing_plugin_setting_uses_the_local_governor_config() -> None:
    assert resolve_config_path("") == ROOT / "config" / "governor.local.yaml"
