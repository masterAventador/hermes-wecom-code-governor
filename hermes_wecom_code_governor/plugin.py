from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import load_governor_config
from .delivery import FileDeliveryService, LazyTencentCosPublisher
from .host import require_supported_hermes_version
from .policy import Identity
from .runtime import GovernorRuntime, SessionEnvironment


def resolve_config_path(configured: object) -> Path:
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[1] / "config" / "governor.local.yaml"


def hermes_session_environment() -> SessionEnvironment:
    from gateway.session_context import get_session_env

    return SessionEnvironment(
        platform=get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower(),
        session_key=get_session_env("HERMES_SESSION_KEY", "").strip(),
        identity=Identity(
            user_id=get_session_env("HERMES_SESSION_USER_ID", "").strip(),
            chat_id=get_session_env("HERMES_SESSION_CHAT_ID", "").strip(),
            chat_type=get_session_env("HERMES_SESSION_CHAT_TYPE", "").strip(),
        ),
        message_id=get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip(),
    )


def _json_handler(callback: Callable[[dict], object]) -> Callable[..., str]:
    def handler(args: dict | None = None, **_: Any) -> str:
        try:
            value = callback(args if isinstance(args, dict) else {})
            return json.dumps(value, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    return handler


def _required_text(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _text(args: dict, key: str, default: str) -> str:
    value = args.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _integer(args: dict, key: str, default: int) -> int:
    value = args.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _boolean(args: dict, key: str, default: bool) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _text_list(args: dict, key: str, default: list[str] | None = None) -> list[str]:
    value = args.get(key, default)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ValueError(f"{key} must be a non-empty string list")
    return [item.strip() for item in value]


def _optional_text_list(args: dict, key: str) -> list[str]:
    if key not in args:
        return []
    return _text_list(args, key)


def _tool_schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def register_runtime_components(ctx: Any, runtime: GovernorRuntime | Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", runtime.pre_gateway_dispatch)
    ctx.register_hook("pre_llm_call", runtime.pre_llm_call)
    ctx.register_hook("pre_tool_call", runtime.pre_tool_call)

    tools = (
        (
            "governor_list_projects",
            "搜索或列出当前企微用户获准访问的项目；项目较多时应使用名称或路径片段搜索。",
            {
                "query": {"type": "string", "description": "可选项目名称或路径片段"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "最多返回数量，默认 20",
                },
            },
            [],
            lambda args: runtime.list_projects(
                query=str(args.get("query", "")).strip(),
                limit=_integer(args, "limit", 20),
            ),
        ),
        (
            "governor_select_project",
            "按 project_id 或显示名称选择/切换一个已授权项目。需要项目操作时必须先选项目。",
            {"project": {"type": "string", "description": "项目显示名称或 project_id"}},
            ["project"],
            lambda args: runtime.select_project(_required_text(args, "project")),
        ),
        (
            "governor_project_files",
            "在当前已选项目内按 glob 列出文件及元数据；可按修改时间排序并计算 SHA-256。",
            {
                "path": {"type": "string", "description": "项目内相对目录，默认 ."},
                "pattern": {"type": "string", "description": "文件 glob，默认 *"},
                "recursive": {"type": "boolean", "description": "是否递归查找"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "最多返回多少个文件",
                },
                "sort": {
                    "type": "string",
                    "enum": ["path", "mtime_desc"],
                    "description": "按路径或修改时间倒序排列",
                },
                "sha256": {"type": "boolean", "description": "是否计算每个文件的 SHA-256"},
            },
            [],
            lambda args: runtime.project_files(
                path=_text(args, "path", "."),
                pattern=_text(args, "pattern", "*"),
                recursive=_boolean(args, "recursive", False),
                limit=_integer(args, "limit", 50),
                sort=_text(args, "sort", "path"),
                sha256=_boolean(args, "sha256", False),
            ),
        ),
        (
            "governor_project_read",
            "批量读取当前已选项目内的文本文件，可限制起始行和行数。",
            {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 10,
                    "description": "最多 10 个项目内相对文件路径",
                },
                "start_line": {"type": "integer", "minimum": 1},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            ["paths"],
            lambda args: runtime.project_read(
                paths=_text_list(args, "paths"),
                start_line=_integer(args, "start_line", 1),
                max_lines=_integer(args, "max_lines", 200),
            ),
        ),
        (
            "governor_project_search",
            "在当前已选项目内按字面文本搜索代码和文档，自动跳过依赖、密钥与二进制文件。",
            {
                "query": {"type": "string", "description": "要搜索的字面文本"},
                "path": {"type": "string", "description": "项目内相对目录，默认 ."},
                "patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 20,
                    "description": "可选文件名 glob 列表",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "case_sensitive": {"type": "boolean"},
            },
            ["query"],
            lambda args: runtime.project_search(
                query=_required_text(args, "query"),
                path=_text(args, "path", "."),
                patterns=(_text_list(args, "patterns") if "patterns" in args else None),
                limit=_integer(args, "limit", 50),
                case_sensitive=_boolean(args, "case_sensitive", False),
            ),
        ),
        (
            "governor_project_git",
            "查看当前已选项目的只读 Git 状态、历史、差异统计或提交摘要。",
            {
                "action": {
                    "type": "string",
                    "enum": ["status", "log", "diff", "show"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "revision": {"type": "string", "description": "show 操作需要的提交引用"},
            },
            ["action"],
            lambda args: runtime.project_git(
                action=_required_text(args, "action"),
                limit=_integer(args, "limit", 20),
                revision=(_required_text(args, "revision") if "revision" in args else None),
            ),
        ),
        (
            "governor_codex_change",
            "创建受控 worktree，委托 Codex App Server 修改和自测，再由脚本验证并合并。",
            {
                "request": {
                    "type": "string",
                    "description": "需要完成的完整代码修改请求",
                },
                "title": {
                    "type": "string",
                    "description": "根据用户要求概括的不超过 12 个字的语义任务名",
                },
            },
            ["request", "title"],
            lambda args: runtime.codex_change(
                _required_text(args, "request"), _required_text(args, "title")
            ),
        ),
        (
            "governor_project_job",
            "在临时隔离 worktree 中执行当前项目预先允许的本地任务；"
            "可用于测试、打包、导出等，产物自动交付。",
            {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 20,
                    "description": "从当前项目允许列表中选择的完整命令参数数组",
                },
                "artifact_globs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 10,
                    "description": "可选；需要自动交付的已允许产物 glob",
                },
                "title": {
                    "type": "string",
                    "description": "不超过 12 个字的本地任务名称",
                },
            },
            ["argv", "title"],
            lambda args: runtime.project_job(
                argv=_text_list(args, "argv"),
                artifact_globs=_optional_text_list(args, "artifact_globs"),
                title=_required_text(args, "title"),
            ),
        ),
        (
            "governor_remote_task",
            "触发当前项目预先登记的远程受控动作；只能按上下文清单里的登记名称触发，"
            "目标主机与命令均固定，不接受自定义命令。",
            {
                "action": {
                    "type": "string",
                    "description": "当前项目 remote_actions 中登记的动作名称",
                }
            },
            ["action"],
            lambda args: runtime.remote_task(_required_text(args, "action")),
        ),
        (
            "governor_deliver_file",
            "仅在用户明确要求时，交付当前已选项目内的现有文件；小文件发企微，大文件发腾讯云临时链接。",
            {
                "path": {
                    "type": "string",
                    "description": "当前已选项目目录内的绝对路径或相对路径",
                }
            },
            ["path"],
            lambda args: runtime.deliver_file(_required_text(args, "path")),
        ),
    )
    for name, description, properties, required, callback in tools:
        ctx.register_tool(
            name=name,
            toolset="code_governor",
            schema=_tool_schema(name, description, properties, required),
            handler=_json_handler(callback),
            check_fn=lambda: True,
            description=description,
            emoji="🛡️",
        )


def register(ctx: Any) -> None:
    require_supported_hermes_version()
    config_path = resolve_config_path(ctx.get_config("config_path", ""))
    config = load_governor_config(config_path)
    delivery = None
    if config.delivery is not None:
        publisher = LazyTencentCosPublisher(config.delivery.cos)
        delivery = FileDeliveryService(
            publisher,
            key_prefix=config.delivery.cos.key_prefix,
            wecom_file_max_bytes=config.delivery.wecom_file_max_bytes,
        )
    runtime = GovernorRuntime(
        config,
        ctx.state,
        env_provider=hermes_session_environment,
        delivery=delivery,
    )
    register_runtime_components(ctx, runtime)

    from .wecom_adapter import register_governed_wecom_platform

    register_governed_wecom_platform(ctx, runtime)
