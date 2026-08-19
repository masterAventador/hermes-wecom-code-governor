from pathlib import Path

import pytest
import yaml

from hermes_wecom_code_governor.config import load_governor_config
from hermes_wecom_code_governor.policy import Identity

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
    assert config.codex.model == "gpt-5.6-sol"
    assert config.codex.reasoning_effort == "xhigh"
    vpp = config.policy.project("vpp-digital-twin")
    assert vpp.job_allowed_commands == (
        ("npm", "test"),
        (
            "npm",
            "run",
            "build:win",
            "--",
            "--config.electronDownload.isVerifyChecksum=false",
        ),
        ("npm", "run", "qa:screenshot"),
    )
    assert vpp.job_gui_commands == (("npm", "run", "qa:screenshot"),)
    assert vpp.job_environment == (
        (
            "VPP_QA_USER_DATA",
            "${JOB_HOME}/Library/Application Support/vpp-digital-twin",
        ),
    )
    assert vpp.job_artifact_globs == ("release/*.exe", "qa-artifacts/screenshots/*.png")
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
