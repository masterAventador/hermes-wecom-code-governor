from dataclasses import dataclass

import pytest

from hermes_wecom_code_governor.policy import Identity
from hermes_wecom_code_governor.selection import (
    ProjectSelectionService,
    SelectionStatus,
)


@dataclass
class FakeClock:
    value: float = 1000.0

    def __call__(self) -> float:
        return self.value


def test_card_selection_is_bound_to_original_user_and_chat() -> None:
    clock = FakeClock()
    service = ProjectSelectionService(clock=clock, ttl_seconds=300)
    owner = Identity("owner", "group-1", "group")
    colleague = Identity("colleague", "group-1", "group")
    other_chat = Identity("owner", "group-2", "group")

    buttons = service.register_card(
        clarify_id="clarify-1",
        session_key="agent:main:wecom:group:group-1:owner",
        identity=owner,
        projects={"aijd-demo": "AIJD测试项目", "vpp": "VPP数字孪生项目"},
    )

    assert [(button.text, button.key) for button in buttons] == [
        ("AIJD测试项目", "aijd-demo"),
        ("VPP数字孪生项目", "vpp"),
    ]
    assert service.resolve_card("clarify-1", colleague, "vpp").status is SelectionStatus.FORBIDDEN
    assert service.resolve_card("clarify-1", other_chat, "vpp").status is SelectionStatus.FORBIDDEN

    selected = service.resolve_card("clarify-1", owner, "vpp")
    assert selected.status is SelectionStatus.SELECTED
    assert selected.project_id == "vpp"
    assert selected.response_text == "VPP数字孪生项目"
    assert service.current_project_id(selected.session_key, owner) == "vpp"


def test_forbidden_click_does_not_consume_pending_card() -> None:
    service = ProjectSelectionService(clock=FakeClock(), ttl_seconds=300)
    owner = Identity("owner", "group-1", "group")
    service.register_card("clarify-1", "session-1", owner, {"vpp": "VPP数字孪生项目"})

    service.resolve_card("clarify-1", Identity("other", "group-1", "group"), "vpp")

    assert service.resolve_card("clarify-1", owner, "vpp").status is SelectionStatus.SELECTED


def test_expired_or_unknown_card_cannot_change_project() -> None:
    clock = FakeClock()
    service = ProjectSelectionService(clock=clock, ttl_seconds=30)
    owner = Identity("owner", "group-1", "group")
    service.register_card("clarify-1", "session-1", owner, {"vpp": "VPP数字孪生项目"})
    clock.value += 31

    assert service.resolve_card("clarify-1", owner, "vpp").status is SelectionStatus.EXPIRED
    assert service.resolve_card("missing", owner, "vpp").status is SelectionStatus.EXPIRED
    assert service.current_project_id("session-1", owner) is None


def test_invalid_project_keeps_card_available_for_valid_retry() -> None:
    service = ProjectSelectionService(clock=FakeClock(), ttl_seconds=300)
    owner = Identity("owner", "group-1", "group")
    service.register_card("clarify-1", "session-1", owner, {"vpp": "VPP数字孪生项目"})

    invalid = service.resolve_card("clarify-1", owner, "not-authorized")
    assert invalid.status is SelectionStatus.INVALID
    assert service.resolve_card("clarify-1", owner, "vpp").status is SelectionStatus.SELECTED


def test_explicit_selection_allows_project_switch_but_only_within_authorized_set() -> None:
    service = ProjectSelectionService(clock=FakeClock(), ttl_seconds=300)
    owner = Identity("owner", "owner", "dm")

    service.select_explicitly("session-1", owner, "aijd-demo", ("aijd-demo", "vpp"))
    assert service.current_project_id("session-1", owner) == "aijd-demo"

    service.select_explicitly("session-1", owner, "vpp", ("aijd-demo", "vpp"))
    assert service.current_project_id("session-1", owner) == "vpp"

    with pytest.raises(PermissionError, match="not authorized"):
        service.select_explicitly("session-1", owner, "secret", ("aijd-demo", "vpp"))
    assert service.current_project_id("session-1", owner) == "vpp"


def test_selected_project_is_not_visible_to_another_identity() -> None:
    service = ProjectSelectionService(clock=FakeClock(), ttl_seconds=300)
    owner = Identity("owner", "group-1", "group")
    service.select_explicitly("session-1", owner, "vpp", ("vpp",))

    assert service.current_project_id("session-1", Identity("other", "group-1", "group")) is None
