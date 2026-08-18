from pathlib import Path

import pytest

from hermes_wecom_code_governor.config import load_governor_config
from hermes_wecom_code_governor.policy import Identity


def test_loads_projects_permissions_commands_and_safety_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "governor.yaml"
    config_path.write_text(
        """
version: 1
runtime_root: /runtime/hermes-governor
safety:
  card_ttl_seconds: 180
  max_changed_files: 40
  max_deleted_files: 3
codex:
  model: gpt-5.6-sol
  reasoning_effort: xhigh
delivery:
  wecom_file_max_bytes: 52428800
  cos:
    bucket: vpp-setup-1424480216
    region: ap-beijing
    key_prefix: electron-builds
    url_expires_seconds: 604800
projects:
  - id: aijd-demo
    name: AIJD测试项目
    path: /workspace/sourceCode/bjx/aijd-demo
    base_branch: dev
    validation_commands:
      - [python, -m, pytest]
  - id: vpp
    name: VPP数字孪生项目
    path: /workspace/sourceCode/vpp-digital-twin
    validation_commands:
      - [npm, test]
    seed_paths:
      - node_modules
    readable_paths:
      - /opt/homebrew
      - /System/Cryptexes
    job:
      allowed_commands:
        - [npm, test]
        - [npm, run, '*']
      artifact_globs:
        - release/*.exe
      timeout_seconds: 1800
      home_seeds:
        - source: /workspace/cache/electron
          target: Library/Caches/electron
      unix_sockets:
        - /private/var/run/mDNSResponder
permissions:
  - name: owner-all-source
    users: [owner]
    chats: ['*']
    roots: [/workspace/sourceCode]
""".strip(),
        encoding="utf-8",
    )

    config = load_governor_config(config_path)

    assert config.runtime_root == Path("/runtime/hermes-governor")
    assert config.safety.card_ttl_seconds == 180
    assert config.safety.max_changed_files == 40
    assert config.safety.max_deleted_files == 3
    assert config.codex.model == "gpt-5.6-sol"
    assert config.codex.reasoning_effort == "xhigh"
    assert config.delivery.wecom_file_max_bytes == 52_428_800
    assert config.delivery.cos.bucket == "vpp-setup-1424480216"
    assert config.delivery.cos.region == "ap-beijing"
    assert config.delivery.cos.key_prefix == "electron-builds"
    assert config.delivery.cos.url_expires_seconds == 604_800
    assert config.policy.authorized_project_ids(Identity("owner", "group-1", "group")) == (
        "aijd-demo",
        "vpp",
    )
    vpp = config.policy.project("vpp")
    assert vpp.base_branch is None
    assert vpp.validation_commands == (("npm", "test"),)
    assert vpp.job_allowed_commands == (("npm", "test"), ("npm", "run", "*"))
    assert vpp.job_artifact_globs == ("release/*.exe",)
    assert vpp.job_timeout_seconds == 1800
    assert vpp.seed_paths == ("node_modules",)
    assert vpp.job_home_seeds == (
        (Path("/workspace/cache/electron"), Path("Library/Caches/electron")),
    )
    assert vpp.readable_paths == (Path("/opt/homebrew"), Path("/System/Cryptexes"))
    assert vpp.job_unix_sockets == (Path("/private/var/run/mDNSResponder"),)


@pytest.mark.parametrize("legacy_key", ["seed_paths", "readable_paths"])
def test_legacy_job_level_seed_and_readable_paths_are_rejected(
    tmp_path: Path, legacy_key: str
) -> None:
    config_path = tmp_path / "governor.yaml"
    config_path.write_text(
        f"""
version: 1
runtime_root: /runtime/hermes-governor
projects:
  - id: vpp
    name: VPP数字孪生项目
    path: /workspace/sourceCode/vpp-digital-twin
    job:
      allowed_commands:
        - [npm, test]
      {legacy_key}:
        - node_modules
permissions:
  - name: owner
    users: [owner]
    chats: ['*']
    projects: ['*']
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=f"projects\\[0\\].{legacy_key}"):
        load_governor_config(config_path)


def test_codex_reasoning_effort_is_configurable(tmp_path: Path) -> None:
    config_path = tmp_path / "governor.yaml"
    config_path.write_text(
        """
version: 1
runtime_root: /runtime/hermes-governor
codex:
  reasoning_effort: high
projects:
  - {id: one, name: One, path: /workspace/one}
permissions:
  - name: owner
    users: [owner]
    chats: ['*']
    projects: ['*']
""".strip(),
        encoding="utf-8",
    )

    config = load_governor_config(config_path)

    assert config.codex.reasoning_effort == "high"


def test_explicit_wildcard_project_permission_grants_all_registered_projects(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "governor.yaml"
    config_path.write_text(
        """
version: 1
runtime_root: /runtime/hermes-governor
projects:
  - {id: one, name: One, path: /workspace/one}
  - {id: two, name: Two, path: /workspace/two}
permissions:
  - name: owner
    users: [owner]
    chats: ['*']
    projects: ['*']
""".strip(),
        encoding="utf-8",
    )

    config = load_governor_config(config_path)

    assert config.policy.authorized_project_ids(Identity("owner", "owner", "dm")) == (
        "one",
        "two",
    )


def test_discovers_git_repositories_below_permission_roots_for_every_group_user(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sourceCode"
    explicit = source_root / "vpp-digital-twin"
    discovered = source_root / "team" / "new-service"
    gitfile_repo = source_root / "mobile-app"
    ignored_dependency = source_root / "node_modules" / "dependency"
    initialized_submodule = discovered / "components" / "shared"
    for repository in (
        explicit,
        discovered,
        gitfile_repo,
        ignored_dependency,
        initialized_submodule,
    ):
        repository.mkdir(parents=True)
    (explicit / ".git").mkdir()
    (discovered / ".git").mkdir()
    (gitfile_repo / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    (ignored_dependency / ".git").mkdir()
    (initialized_submodule / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    (discovered / ".gitmodules").write_text(
        '[submodule "shared"]\n\tpath = components/shared\n\turl = local\n',
        encoding="utf-8",
    )

    config_path = tmp_path / "governor.yaml"
    config_path.write_text(
        f"""
version: 1
runtime_root: {tmp_path / "runtime"}
project_discovery:
  enabled: true
projects:
  - id: vpp
    name: VPP数字孪生项目
    path: {explicit}
permissions:
  - name: trusted-source-users
    users: [owner, trusted-colleague]
    chats: ['*']
    roots: [{source_root}]
""".strip(),
        encoding="utf-8",
    )

    config = load_governor_config(config_path)
    projects_by_path = {project.path: project for project in config.policy.projects}

    assert projects_by_path[explicit].display_name == "VPP数字孪生项目"
    assert projects_by_path[explicit].auto_discovered is False
    assert projects_by_path[discovered].display_name == "team/new-service"
    assert projects_by_path[discovered].auto_discovered is True
    assert projects_by_path[gitfile_repo].display_name == "mobile-app"
    assert projects_by_path[initialized_submodule].display_name == (
        "team/new-service/components/shared"
    )
    assert ignored_dependency not in projects_by_path
    expected_ids = tuple(sorted(project.project_id for project in projects_by_path.values()))
    assert config.policy.authorized_project_ids(Identity("owner", "any-chat", "group")) == (
        expected_ids
    )
    assert (
        config.policy.authorized_project_ids(
            Identity("trusted-colleague", "trusted-colleague", "dm")
        )
        == expected_ids
    )
    assert config.policy.authorized_project_ids(Identity("stranger", "any-chat", "group")) == ()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            """
version: 1
runtime_root: relative/runtime
projects: []
permissions: []
""",
            "runtime_root",
        ),
        (
            """
version: 1
runtime_root: /runtime
projects:
  - {id: one, name: One, path: relative/project}
permissions: []
""",
            "project path",
        ),
        (
            """
version: 1
runtime_root: /runtime
projects:
  - {id: same, name: One, path: /workspace/one}
  - {id: same, name: Two, path: /workspace/two}
permissions: []
""",
            "duplicate project id",
        ),
        (
            """
version: 1
runtime_root: /runtime
projects:
  - {id: one, name: Same, path: /workspace/one}
  - {id: two, name: Same, path: /workspace/two}
permissions: []
""",
            "duplicate project name",
        ),
        (
            """
version: 1
runtime_root: /runtime
projects:
  - id: one
    name: One
    path: /workspace/one
    validation_commands: ['npm test']
permissions: []
""",
            "argv list",
        ),
        (
            """
version: 1
runtime_root: /runtime
projects:
  - id: one
    name: One
    path: /workspace/one
    job:
      allowed_commands: [[npm, test]]
      seed_paths: [../outside]
permissions: []
""",
            "seed_paths",
        ),
        (
            """
version: 1
runtime_root: /runtime
projects:
  - id: one
    name: One
    path: /workspace/one
    job:
      allowed_commands: [[npm, test]]
      timeout_seconds: 0
permissions: []
""",
            "timeout_seconds",
        ),
        (
            """
version: 1
runtime_root: /runtime
projects:
  - {id: one, name: One, path: /workspace/one}
permissions:
  - {name: nobody, users: [], chats: ['*'], projects: ['*']}
""",
            "users",
        ),
        (
            """
version: 2
runtime_root: /runtime
projects: []
permissions: []
""",
            "version",
        ),
    ],
)
def test_invalid_or_ambiguous_configuration_fails_closed(
    tmp_path: Path,
    body: str,
    message: str,
) -> None:
    config_path = tmp_path / "governor.yaml"
    config_path.write_text(body.strip(), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_governor_config(config_path)
