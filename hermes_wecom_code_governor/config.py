from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .codex_runtime import CodexRuntimeSettings
from .delivery import WECOM_FILE_MAX_BYTES, CosDeliveryConfig, DeliveryConfig
from .discovery import discover_git_repositories
from .policy import (
    HttpAction,
    HttpActionParameter,
    PermissionGroup,
    Policy,
    Project,
    RemoteAction,
)


@dataclass(frozen=True)
class SafetyConfig:
    card_ttl_seconds: int = 300
    max_changed_files: int = 100
    max_deleted_files: int = 5


@dataclass(frozen=True)
class ProjectDiscoveryConfig:
    enabled: bool = False
    max_projects: int = 500


@dataclass(frozen=True)
class GovernorConfig:
    runtime_root: Path
    safety: SafetyConfig
    policy: Policy
    codex: CodexRuntimeSettings = CodexRuntimeSettings()
    delivery: DeliveryConfig | None = None
    project_discovery: ProjectDiscoveryConfig = ProjectDiscoveryConfig()


def build_project_catalog(
    explicit_projects: tuple[Project, ...],
    permissions: tuple[PermissionGroup, ...],
    discovery: ProjectDiscoveryConfig,
    *,
    runtime_root: Path,
) -> tuple[Project, ...]:
    projects = list(explicit_projects)
    if not discovery.enabled:
        return tuple(projects)

    roots = tuple(
        sorted(
            {root for permission in permissions for root in permission.root_paths},
            key=str,
        )
    )
    repositories = discover_git_repositories(
        roots,
        excluded_paths=(runtime_root,),
        max_projects=discovery.max_projects,
    )
    paths = {project.path for project in projects}
    names = {project.display_name for project in projects}
    ids = {project.project_id for project in projects}
    for repository in repositories:
        if repository.path in paths:
            continue
        project_id = repository.project_id
        if project_id in ids:
            raise ValueError(f"discovered duplicate project id: {project_id}")
        display_name = repository.display_name
        if display_name in names:
            display_name = f"{display_name} [{project_id[-6:]}]"
        projects.append(
            Project(
                project_id=project_id,
                display_name=display_name,
                path=repository.path,
                auto_discovered=True,
            )
        )
        paths.add(repository.path)
        names.add(display_name)
        ids.add(project_id)
    return tuple(projects)


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return value


def _list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return value


def _required_string(data: Mapping[str, Any], key: str, field_name: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _absolute_path(value: Any, field_name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field_name} must be an absolute path")
    return path.resolve()


def _positive_int(data: Mapping[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"safety.{key} must be a positive integer")
    return value


def _positive_int_field(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _boolean_field(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _argv(value: Any, field_name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(token, str) and token for token in value)
    ):
        raise ValueError(f"{field_name} must be a non-empty argv list")
    return tuple(value)


def _commands(value: Any, field_name: str) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    return tuple(
        _argv(command, f"{field_name}[{index}]")
        for index, command in enumerate(_list(value, field_name))
    )


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _relative_path(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty relative path")
    normalized = value.strip()
    path = Path(normalized)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise ValueError(f"{field_name} must stay inside the isolated job directory")
    return normalized


def _relative_paths(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(
        _relative_path(item, f"{field_name}[{index}]")
        for index, item in enumerate(_list(value, field_name))
    )


def _home_seeds(value: Any, field_name: str) -> tuple[tuple[Path, Path], ...]:
    if value is None:
        return ()
    seeds: list[tuple[Path, Path]] = []
    for index, raw_seed in enumerate(_list(value, field_name)):
        prefix = f"{field_name}[{index}]"
        seed = _mapping(raw_seed, prefix)
        source = _absolute_path(seed.get("source"), f"{prefix}.source")
        target = Path(_relative_path(seed.get("target"), f"{prefix}.target"))
        seeds.append((source, target))
    return tuple(seeds)


_RESERVED_JOB_ENV_NAMES = frozenset(
    {
        "HOME",
        "PATH",
        "TMPDIR",
        "SHELL",
        "USER",
        "LOGNAME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "npm_config_cache",
        "ELECTRON_DISABLE_SANDBOX",
    }
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _job_environment(value: Any, field_name: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    data = _mapping(value, field_name)
    entries: list[tuple[str, str]] = []
    for name, raw_value in data.items():
        if (
            not isinstance(name, str)
            or not _ENV_NAME.fullmatch(name)
            or name in _RESERVED_JOB_ENV_NAMES
        ):
            raise ValueError(f"{field_name} contains a reserved or invalid variable name: {name}")
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ValueError(f"{field_name}.{name} must be a non-empty string")
        entries.append((name, raw_value))
    return tuple(entries)


def _remote_actions(value: Any, field_name: str) -> tuple[RemoteAction, ...]:
    if value is None:
        return ()
    actions: list[RemoteAction] = []
    for index, raw_action in enumerate(_list(value, field_name)):
        prefix = f"{field_name}[{index}]"
        data = _mapping(raw_action, prefix)
        actions.append(
            RemoteAction(
                name=_required_string(data, "name", f"{prefix}.name"),
                host=_required_string(data, "host", f"{prefix}.host"),
                argv=_argv(data.get("argv"), f"{prefix}.argv"),
                timeout_seconds=_positive_int_field(
                    data.get("timeout_seconds", 30), f"{prefix}.timeout_seconds"
                ),
            )
        )
    names = [action.name for action in actions]
    if len(set(names)) != len(names):
        raise ValueError(f"{field_name} contains duplicate action names")
    return tuple(actions)


_HTTP_PARAM_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
# choice 值出现在 URL/JSON 模板里，只放行确定无注入面的字符。
_HTTP_CHOICE_VALUE = re.compile(r"^[A-Za-z0-9_#.-]+$")
_HTTP_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


def _http_action_parameters(value: Any, field_name: str) -> tuple[HttpActionParameter, ...]:
    if value is None:
        return ()
    parameters: list[HttpActionParameter] = []
    for index, raw in enumerate(_list(value, field_name)):
        prefix = f"{field_name}[{index}]"
        data = _mapping(raw, prefix)
        name = _required_string(data, "name", f"{prefix}.name")
        if not _HTTP_PARAM_NAME.fullmatch(name):
            raise ValueError(f"{prefix}.name must match [a-z][a-z0-9_]*: {name}")
        kind = _required_string(data, "type", f"{prefix}.type")
        if kind == "integer":
            minimum = _positive_int_field(data.get("minimum", 0) or 0, f"{prefix}.minimum")
            maximum = _positive_int_field(data.get("maximum", 0) or 0, f"{prefix}.maximum")
            if "minimum" not in data or "maximum" not in data or minimum > maximum:
                raise ValueError(f"{prefix} integer parameters require minimum <= maximum bounds")
            parameters.append(
                HttpActionParameter(name=name, type="integer", minimum=minimum, maximum=maximum)
            )
        elif kind == "choice":
            choices = _list(data.get("choices"), f"{prefix}.choices")
            if not choices or not all(
                isinstance(choice, str) and _HTTP_CHOICE_VALUE.fullmatch(choice)
                for choice in choices
            ):
                raise ValueError(
                    f"{prefix}.choices must be non-empty values matching [A-Za-z0-9_#.-]+"
                )
            parameters.append(HttpActionParameter(name=name, type="choice", choices=tuple(choices)))
        else:
            raise ValueError(f"{prefix}.type must be integer or choice: {kind}")
    names = [parameter.name for parameter in parameters]
    if len(set(names)) != len(names):
        raise ValueError(f"{field_name} contains duplicate parameter names")
    return tuple(parameters)


def _template_placeholders(template: str, field_name: str) -> set[str]:
    stripped = template.replace("{{", "").replace("}}", "")
    names = set()
    for match in _HTTP_PLACEHOLDER.finditer(stripped):
        name = match.group(1)
        if not _HTTP_PARAM_NAME.fullmatch(name):
            raise ValueError(f"{field_name} contains an invalid placeholder: {{{name}}}")
        names.add(name)
    return names


def _http_actions(value: Any, field_name: str) -> tuple[HttpAction, ...]:
    if value is None:
        return ()
    actions: list[HttpAction] = []
    for index, raw_action in enumerate(_list(value, field_name)):
        prefix = f"{field_name}[{index}]"
        data = _mapping(raw_action, prefix)
        method = _required_string(data, "method", f"{prefix}.method").upper()
        if method not in {"GET", "POST"}:
            raise ValueError(f"{prefix}.method must be GET or POST")
        url = _required_string(data, "url", f"{prefix}.url")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"{prefix}.url must start with http:// or https://")
        body_template = data.get("body_template")
        if body_template is not None and (
            not isinstance(body_template, str) or not body_template.strip()
        ):
            raise ValueError(f"{prefix}.body_template must be a non-empty string")
        if body_template is not None and method != "POST":
            raise ValueError(f"{prefix}.body_template is only allowed for POST actions")
        parameters = _http_action_parameters(data.get("parameters"), f"{prefix}.parameters")
        placeholders = _template_placeholders(url, f"{prefix}.url")
        if body_template is not None:
            placeholders |= _template_placeholders(body_template, f"{prefix}.body_template")
        declared = {parameter.name for parameter in parameters}
        if placeholders != declared:
            raise ValueError(
                f"{prefix} placeholders {sorted(placeholders)} must exactly match "
                f"declared parameters {sorted(declared)}"
            )
        actions.append(
            HttpAction(
                name=_required_string(data, "name", f"{prefix}.name"),
                method=method,
                url=url,
                body_template=body_template,
                parameters=parameters,
                timeout_seconds=_positive_int_field(
                    data.get("timeout_seconds", 15), f"{prefix}.timeout_seconds"
                ),
            )
        )
    names = [action.name for action in actions]
    if len(set(names)) != len(names):
        raise ValueError(f"{field_name} contains duplicate action names")
    return tuple(actions)


def _parse_project(raw: Any, index: int) -> Project:
    data = _mapping(raw, f"projects[{index}]")
    prefix = f"projects[{index}]"
    job = data.get("job")
    job_data = _mapping(job, f"{prefix}.job") if job is not None else {}
    for moved_key in ("seed_paths", "readable_paths"):
        if moved_key in job_data:
            raise ValueError(
                f"{prefix}.job.{moved_key} has moved to the project level: {prefix}.{moved_key}"
            )
    return Project(
        project_id=_required_string(data, "id", f"{prefix}.id"),
        display_name=_required_string(data, "name", f"{prefix}.name"),
        path=_absolute_path(data.get("path"), f"project path {prefix}.path"),
        base_branch=_optional_string(data.get("base_branch"), f"{prefix}.base_branch"),
        validation_commands=_commands(
            data.get("validation_commands"), f"{prefix}.validation_commands"
        ),
        seed_paths=_relative_paths(data.get("seed_paths"), f"{prefix}.seed_paths"),
        readable_paths=tuple(
            _absolute_path(value, f"{prefix}.readable_paths[{path_index}]")
            for path_index, value in enumerate(
                _list(data.get("readable_paths", []), f"{prefix}.readable_paths")
            )
        ),
        job_allowed_commands=_commands(
            job_data.get("allowed_commands"), f"{prefix}.job.allowed_commands"
        ),
        job_gui_commands=_commands(job_data.get("gui_commands"), f"{prefix}.job.gui_commands"),
        job_trusted_commands=_commands(
            job_data.get("trusted_commands"), f"{prefix}.job.trusted_commands"
        ),
        job_environment=_job_environment(job_data.get("environment"), f"{prefix}.job.environment"),
        job_trusted_environment=_job_environment(
            job_data.get("trusted_environment"), f"{prefix}.job.trusted_environment"
        ),
        job_artifact_globs=_relative_paths(
            job_data.get("artifact_globs"), f"{prefix}.job.artifact_globs"
        ),
        job_timeout_seconds=(
            _positive_int_field(
                job_data.get("timeout_seconds", 1800),
                f"{prefix}.job.timeout_seconds",
            )
            if job_data
            else 1800
        ),
        job_home_seeds=_home_seeds(job_data.get("home_seeds"), f"{prefix}.job.home_seeds"),
        job_trusted_home_seeds=_home_seeds(
            job_data.get("trusted_home_seeds"), f"{prefix}.job.trusted_home_seeds"
        ),
        remote_actions=_remote_actions(data.get("remote_actions"), f"{prefix}.remote_actions"),
        http_actions=_http_actions(data.get("http_actions"), f"{prefix}.http_actions"),
        push_on_merge=_boolean_field(data.get("push_on_merge", False), f"{prefix}.push_on_merge"),
        job_unix_sockets=tuple(
            _absolute_path(value, f"{prefix}.job.unix_sockets[{socket_index}]")
            for socket_index, value in enumerate(
                _list(job_data.get("unix_sockets", []), f"{prefix}.job.unix_sockets")
            )
        ),
    )


def _string_set(value: Any, field_name: str, *, allow_empty: bool = False) -> frozenset[str]:
    items = _list(value, field_name)
    if not allow_empty and not items:
        raise ValueError(f"{field_name} must contain at least one value")
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise ValueError(f"{field_name} must contain non-empty strings")
    return frozenset(item.strip() for item in items)


def _parse_permission(raw: Any, index: int) -> PermissionGroup:
    data = _mapping(raw, f"permissions[{index}]")
    prefix = f"permissions[{index}]"
    roots = tuple(
        _absolute_path(value, f"{prefix}.roots[{root_index}]")
        for root_index, value in enumerate(_list(data.get("roots", []), f"{prefix}.roots"))
    )
    project_ids = _string_set(data.get("projects", []), f"{prefix}.projects", allow_empty=True)
    if not roots and not project_ids:
        raise ValueError(f"{prefix} must grant projects or roots")
    return PermissionGroup(
        name=_required_string(data, "name", f"{prefix}.name"),
        user_ids=_string_set(data.get("users"), f"{prefix}.users"),
        chat_ids=_string_set(data.get("chats"), f"{prefix}.chats"),
        project_ids=project_ids,
        root_paths=roots,
    )


def load_governor_config(path: Path) -> GovernorConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    data = _mapping(raw, "config")
    if data.get("version") != 1:
        raise ValueError("config version must be 1")

    runtime_root = _absolute_path(data.get("runtime_root"), "runtime_root")
    safety_data = _mapping(data.get("safety", {}), "safety")
    safety = SafetyConfig(
        card_ttl_seconds=_positive_int(safety_data, "card_ttl_seconds", 300),
        max_changed_files=_positive_int(safety_data, "max_changed_files", 100),
        max_deleted_files=_positive_int(safety_data, "max_deleted_files", 5),
    )
    codex_data = _mapping(data.get("codex", {}), "codex")
    codex_model = codex_data.get("model", "gpt-5.6-sol")
    if not isinstance(codex_model, str) or not codex_model.strip():
        raise ValueError("codex.model must be a non-empty string")
    reasoning_effort = codex_data.get("reasoning_effort", "xhigh")
    valid_reasoning_efforts = {"low", "medium", "high", "xhigh", "max", "ultra"}
    if reasoning_effort not in valid_reasoning_efforts:
        raise ValueError("codex.reasoning_effort is invalid")
    raw_codex_bin = codex_data.get("binary")
    codex_bin = _absolute_path(raw_codex_bin, "codex.binary") if raw_codex_bin is not None else None
    codex = CodexRuntimeSettings(codex_model.strip(), reasoning_effort, codex_bin)
    raw_delivery = data.get("delivery")
    delivery = None
    if raw_delivery is not None:
        delivery_data = _mapping(raw_delivery, "delivery")
        cos_data = _mapping(delivery_data.get("cos"), "delivery.cos")
        wecom_file_max_bytes = _positive_int_field(
            delivery_data.get("wecom_file_max_bytes", WECOM_FILE_MAX_BYTES),
            "delivery.wecom_file_max_bytes",
        )
        if wecom_file_max_bytes > WECOM_FILE_MAX_BYTES:
            raise ValueError("delivery.wecom_file_max_bytes cannot exceed 50 MiB")
        delivery = DeliveryConfig(
            wecom_file_max_bytes=wecom_file_max_bytes,
            cos=CosDeliveryConfig(
                bucket=_required_string(cos_data, "bucket", "delivery.cos.bucket"),
                region=_required_string(cos_data, "region", "delivery.cos.region"),
                key_prefix=_required_string(cos_data, "key_prefix", "delivery.cos.key_prefix"),
                url_expires_seconds=_positive_int_field(
                    cos_data.get("url_expires_seconds", 604_800),
                    "delivery.cos.url_expires_seconds",
                ),
            ),
        )
    explicit_projects = tuple(
        _parse_project(raw_project, index)
        for index, raw_project in enumerate(_list(data.get("projects"), "projects"))
    )
    project_ids = [project.project_id for project in explicit_projects]
    if len(set(project_ids)) != len(project_ids):
        raise ValueError("duplicate project id")
    project_names = [project.display_name for project in explicit_projects]
    if len(set(project_names)) != len(project_names):
        raise ValueError("duplicate project name")

    permissions = tuple(
        _parse_permission(raw_permission, index)
        for index, raw_permission in enumerate(_list(data.get("permissions"), "permissions"))
    )
    discovery_data = _mapping(data.get("project_discovery", {}), "project_discovery")
    project_discovery = ProjectDiscoveryConfig(
        enabled=_boolean_field(
            discovery_data.get("enabled", False),
            "project_discovery.enabled",
        ),
        max_projects=_positive_int_field(
            discovery_data.get("max_projects", 500),
            "project_discovery.max_projects",
        ),
    )
    projects = build_project_catalog(
        explicit_projects,
        permissions,
        project_discovery,
        runtime_root=runtime_root,
    )
    return GovernorConfig(
        runtime_root=runtime_root,
        safety=safety,
        policy=Policy(projects=projects, permission_groups=permissions),
        codex=codex,
        delivery=delivery,
        project_discovery=project_discovery,
    )
