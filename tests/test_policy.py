from pathlib import Path

from hermes_wecom_code_governor.policy import (
    Identity,
    PermissionGroup,
    Policy,
    Project,
)


def _project(project_id: str, path: str) -> Project:
    return Project(project_id=project_id, display_name=project_id, path=Path(path))


def test_user_and_chat_must_both_match_for_project_permission() -> None:
    aijd = _project("aijd-demo", "/workspace/sourceCode/bjx/aijd-demo")
    policy = Policy(
        projects=(aijd,),
        permission_groups=(
            PermissionGroup(
                name="aijd-group",
                user_ids=frozenset({"user-owner"}),
                chat_ids=frozenset({"chat-aijd"}),
                project_ids=frozenset({"aijd-demo"}),
            ),
        ),
    )

    assert policy.authorized_project_ids(Identity("user-owner", "chat-aijd", "group")) == (
        "aijd-demo",
    )
    assert policy.authorized_project_ids(Identity("user-owner", "chat-other", "group")) == ()
    assert policy.authorized_project_ids(Identity("user-other", "chat-aijd", "group")) == ()


def test_wildcard_chat_allows_same_user_in_dm_and_groups() -> None:
    aijd = _project("aijd-demo", "/workspace/sourceCode/bjx/aijd-demo")
    policy = Policy(
        projects=(aijd,),
        permission_groups=(
            PermissionGroup(
                name="owner",
                user_ids=frozenset({"user-owner"}),
                chat_ids=frozenset({"*"}),
                project_ids=frozenset({"aijd-demo"}),
            ),
        ),
    )

    assert policy.is_authorized(Identity("user-owner", "user-owner", "dm"))
    assert policy.is_authorized(Identity("user-owner", "chat-any", "group"))
    assert not policy.is_authorized(Identity("user-other", "chat-any", "group"))


def test_root_permission_only_includes_registered_projects_below_root() -> None:
    aijd = _project("aijd-demo", "/workspace/sourceCode/bjx/aijd-demo")
    vpp = _project("vpp", "/workspace/sourceCode/vpp-digital-twin")
    outside = _project("outside", "/workspace/other/secret-project")
    policy = Policy(
        projects=(outside, vpp, aijd),
        permission_groups=(
            PermissionGroup(
                name="source-owner",
                user_ids=frozenset({"user-owner"}),
                chat_ids=frozenset({"*"}),
                root_paths=(Path("/workspace/sourceCode"),),
            ),
        ),
    )

    assert policy.authorized_project_ids(Identity("user-owner", "chat-any", "group")) == (
        "aijd-demo",
        "vpp",
    )


def test_unknown_project_id_does_not_grant_access() -> None:
    policy = Policy(
        projects=(_project("aijd-demo", "/workspace/sourceCode/bjx/aijd-demo"),),
        permission_groups=(
            PermissionGroup(
                name="bad-config",
                user_ids=frozenset({"user-owner"}),
                chat_ids=frozenset({"chat-aijd"}),
                project_ids=frozenset({"missing-project"}),
            ),
        ),
    )

    identity = Identity("user-owner", "chat-aijd", "group")
    assert policy.authorized_project_ids(identity) == ()
    assert not policy.is_authorized(identity)
