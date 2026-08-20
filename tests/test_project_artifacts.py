from pathlib import Path

import pytest
import yaml

from hermes_wecom_code_governor.config import load_governor_config
from hermes_wecom_code_governor.policy import Identity, RemoteAction

ROOT = Path(__file__).resolve().parents[1]


def test_local_governor_config_contains_known_projects_and_no_secrets() -> None:
    path = ROOT / "config" / "governor.local.yaml"
    text = path.read_text(encoding="utf-8")
    config = load_governor_config(path)

    project_ids = {project.project_id for project in config.policy.projects}
    assert {
        "aijd-demo",
        "vpp-digital-twin",
    }.issubset(project_ids)
    assert config.project_discovery.enabled
    assert not any(project.auto_discovered for project in config.policy.projects)
    assert config.policy.permission_groups[0].user_ids == frozenset(
        {"woay8AEgAAw8MXiWiwMvDhA3H4jAQJWg"}
    )
    assert config.policy.permission_groups[0].chat_ids == frozenset({"*"})
    assert config.policy.permission_groups[0].root_paths == ()
    assert config.policy.authorized_project_ids(
        Identity("woay8AEgAAw8MXiWiwMvDhA3H4jAQJWg", "any-chat", "group")
    ) == ("vpp-digital-twin",)
    # 同事仅限测试群内使用，且只有 VPP 项目权限。
    assert config.policy.authorized_project_ids(
        Identity(
            "woay8AEgAAZfzy3jtAr2Hg2bavWQUWKA",
            "wray8AEgAA2JNIuggrnHNURw-4Y1be8Q",
            "group",
        )
    ) == ("vpp-digital-twin",)
    assert not config.policy.is_authorized(
        Identity("woay8AEgAAZfzy3jtAr2Hg2bavWQUWKA", "other-chat", "group")
    )
    assert not config.policy.is_authorized(
        Identity(
            "woay8AEgAAZfzy3jtAr2Hg2bavWQUWKA",
            "woay8AEgAAZfzy3jtAr2Hg2bavWQUWKA",
            "dm",
        )
    )
    assert config.policy.authorized_project_ids(
        Identity(
            "woay8AEgAA9pDEkYK11PFqReBQwSoRAg",
            "wray8AEgAAS_N13q4DAUbSk_fWnSHJhg",
            "group",
        )
    ) == ("vpp-digital-twin",)
    assert not config.policy.is_authorized(
        Identity("woay8AEgAA9pDEkYK11PFqReBQwSoRAg", "other-chat", "group")
    )
    assert config.codex.model == "gpt-5.6-sol"
    assert config.codex.reasoning_effort == "xhigh"
    vpp = config.policy.project("vpp-digital-twin")
    assert vpp.remote_actions == (
        RemoteAction(
            name="生成激活码",
            host="vpp-license",
            argv=("/usr/local/bin/node", "/opt/vpp-license/issue-code.mjs"),
            timeout_seconds=30,
        ),
    )
    assert vpp.push_on_merge is True
    assert vpp.job_allowed_commands == (
        ("npm", "test"),
        (
            "npm",
            "run",
            "build:win",
            "--",
            "--config.electronDownload.isVerifyChecksum=false",
        ),
        ("npm", "run", "build:mac"),
        ("npm", "run", "qa:screenshot"),
    )
    assert vpp.job_gui_commands == (("npm", "run", "qa:screenshot"),)
    assert vpp.job_trusted_commands == (("npm", "run", "build:mac"),)
    assert vpp.job_environment == (
        (
            "VPP_QA_USER_DATA",
            "${JOB_HOME}/Library/Application Support/vpp-digital-twin",
        ),
    )
    # mac 签名走真实 HOME 的登录钥匙串（受信档与用户终端同构），
    # 不向任何任务环境注入签名材料。
    assert vpp.job_trusted_environment == ()
    assert vpp.job_trusted_home_seeds == ()
    assert vpp.job_artifact_globs == (
        "release/*.exe",
        "release/*.dmg",
        "qa-artifacts/screenshots/*.png",
    )
    assert (
        Path("/Users/aventador/Library/Application Support/vpp-digital-twin/licensing.json"),
        Path("Library/Application Support/vpp-digital-twin/licensing.json"),
    ) in vpp.job_home_seeds
    assert vpp.seed_paths == ("node_modules",)
    assert vpp.readable_paths == (
        Path("/opt/homebrew"),
        Path("/System/Cryptexes"),
        Path("/System/Volumes/Preboot/Cryptexes/OS"),
    )
    assert vpp.job_unix_sockets == (Path("/private/var/run/mDNSResponder"),)
    assert "\n    package:" not in text
    for forbidden in ("SecretId", "SecretKey", "WECOM_SECRET", "AKID"):
        assert forbidden not in text


def test_sensitive_environment_values_must_come_from_secret_files() -> None:
    config = load_governor_config(ROOT / "config" / "governor.local.yaml")

    # 名字像密钥的环境变量禁止在入库配置里写明文，必须走 SECRET_FILE。
    for project in config.policy.projects:
        for name, value in (*project.job_environment, *project.job_trusted_environment):
            if any(marker in name.upper() for marker in ("PASSWORD", "SECRET", "TOKEN")):
                assert value.startswith("${SECRET_FILE:"), (
                    f"{project.project_id}.{name} 必须使用 SECRET_FILE，不允许明文"
                )


def test_secret_file_values_may_only_live_in_the_trusted_environment() -> None:
    config = load_governor_config(ROOT / "config" / "governor.local.yaml")

    # SECRET_FILE 值出现在普通 environment 就是把密钥下发给全部沙箱任务，
    # 与项目数无关的通用不变量：密钥值只允许进受信桶。
    for project in config.policy.projects:
        for name, value in project.job_environment:
            assert not value.startswith("${SECRET_FILE:"), (
                f"{project.project_id}.{name} 是密钥值，必须放 trusted_environment"
            )


def test_plain_home_seeds_must_not_come_from_secret_directories() -> None:
    from hermes_wecom_code_governor.sandbox_profile import _HOME_SECRET_DIRS

    config = load_governor_config(ROOT / "config" / "governor.local.yaml")
    home = Path("/Users/aventador")
    denied_roots = tuple(home / relative for relative in _HOME_SECRET_DIRS)

    # 拒读名单目录里的文件属于密钥材料，只允许经 trusted_home_seeds 下发。
    for project in config.policy.projects:
        for source, _target in project.job_home_seeds:
            assert not any(source.is_relative_to(root) for root in denied_roots), (
                f"{project.project_id} 普通种子 {source} 来自密钥目录，必须改放 trusted_home_seeds"
            )


def test_secret_sources_are_denied_to_sandboxed_jobs() -> None:
    from hermes_wecom_code_governor.sandbox_profile import _HOME_SECRET_DIRS

    config = load_governor_config(ROOT / "config" / "governor.local.yaml")
    home = Path("/Users/aventador")
    denied_roots = tuple(home / relative for relative in _HOME_SECRET_DIRS)

    # 受信种子与 SECRET_FILE 引用的每个来源都必须落在沙箱拒读名单内，
    # 否则普通沙箱任务可以直接读走签名材料。
    for project in config.policy.projects:
        for source, _target in project.job_trusted_home_seeds:
            assert any(source.is_relative_to(root) for root in denied_roots), (
                f"{project.project_id} 受信种子 {source} 不在沙箱拒读名单内"
            )
        for name, value in (*project.job_environment, *project.job_trusted_environment):
            if value.startswith("${SECRET_FILE:") and value.endswith("}"):
                secret_path = Path(value[len("${SECRET_FILE:") : -1])
                assert any(secret_path.is_relative_to(root) for root in denied_roots), (
                    f"{project.project_id}.{name} 的密钥文件 {secret_path} 不在沙箱拒读名单内"
                )


def test_hermes_config_patch_pins_subscription_model_and_plugin() -> None:
    data = yaml.safe_load((ROOT / "config" / "hermes.config.local.yaml").read_text())

    assert data["model"] == {
        "provider": "openai-codex",
        "default": "gpt-5.6-sol",
        "openai_runtime": "auto",
    }
    assert data["agent"]["reasoning_effort"] == "medium"
    assert data["timeouts"]["tools"]["concurrent_batch"] == 7200
    assert data["model"].get("openai_runtime", "auto") == "auto"
    assert "hermes-wecom-code-governor" in data["plugins"]["enabled"]
    # 路由层只认顶层开关；extra 里的同名开关只影响适配器合批，两处必须一致。
    assert data["group_sessions_per_user"] is False
    # 本机 Fake-IP 代理环境下必须放行私网解析，否则企微图片下载全被 SSRF 拦截。
    assert data["security"]["allow_private_urls"] is True
    # 关掉自动重置：避免 2 小时清空上下文，也避免网关内置重置横幅泄露模型/厂商。
    assert data["session_reset"]["mode"] == "none"
    assert data["session_reset"]["notify"] is False
    assert data["gateway"]["platforms"]["wecom"]["extra"]["group_sessions_per_user"] is False
    assert "secret" not in data["gateway"]["platforms"]["wecom"]["extra"]

    example = yaml.safe_load((ROOT / "config" / "hermes.config.example.yaml").read_text())
    assert example["agent"]["reasoning_effort"] == "medium"


def test_hermes_platform_fallback_intake_only_allows_the_owner() -> None:
    data = yaml.safe_load((ROOT / "config" / "hermes.config.local.yaml").read_text())
    extra = data["gateway"]["platforms"]["wecom"]["extra"]

    owner = "woay8AEgAAw8MXiWiwMvDhA3H4jAQJWg"
    assert extra["dm_policy"] == "allowlist"
    assert extra["allow_from"] == [owner]
    assert extra["group_policy"] == "allowlist"
    assert extra["group_allow_from"] == ["*"]
    assert extra["groups"]["*"]["allow_from"] == [owner]


def test_plugin_manifest_declares_hooks_and_tools() -> None:
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))

    assert manifest["name"] == "hermes-wecom-code-governor"
    assert set(manifest["hooks"]) == {
        "pre_gateway_dispatch",
        "pre_llm_call",
        "pre_tool_call",
    }
    assert set(manifest["provides_tools"]) == {
        "governor_list_projects",
        "governor_select_project",
        "governor_project_files",
        "governor_project_read",
        "governor_project_search",
        "governor_project_git",
        "governor_codex_change",
        "governor_project_job",
        "governor_remote_task",
        "governor_push",
        "governor_deliver_file",
    }


def test_python_package_declares_codex_sdk_and_explicit_package_discovery() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "openai-codex" in pyproject
    assert "[tool.setuptools.packages.find]" in pyproject
    assert 'include = ["hermes_wecom_code_governor*"]' in pyproject


@pytest.mark.parametrize(
    ("script_name", "command"),
    (
        ("install-service.sh", "gateway install --force --start-now --start-on-login"),
        ("start.sh", "gateway start"),
        ("stop.sh", "gateway stop"),
        ("status.sh", "gateway status --deep --full"),
    ),
)
def test_local_service_scripts_use_the_isolated_runtime(
    script_name: str,
    command: str,
) -> None:
    path = ROOT / "scripts" / script_name
    text = path.read_text(encoding="utf-8")

    assert "set -euo pipefail" in text
    assert ".runtime/hermes-home" in text
    assert "/Users/aventador/.hermes/hermes-agent/venv/bin/hermes" in text
    assert command in text
    assert "WECOM_SECRET=" not in text
