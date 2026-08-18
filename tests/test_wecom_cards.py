from hermes_wecom_code_governor.wecom_cards import (
    CardEvent,
    match_project_choices,
    parse_card_event,
)


def test_only_exact_authorized_project_choices_become_a_card() -> None:
    projects = {"aijd-demo": "AIJD测试项目", "vpp": "VPP数字孪生项目"}

    assert match_project_choices(["AIJD测试项目 (Recommended)", "VPP数字孪生项目"], projects) == (
        ("aijd-demo", "AIJD测试项目"),
        ("vpp", "VPP数字孪生项目"),
    )
    assert match_project_choices(["是", "否"], projects) is None
    assert match_project_choices(["AIJD测试项目"], projects) == (("aijd-demo", "AIJD测试项目"),)
    assert match_project_choices(["AIJD测试项目", "VPP数字孪生项目", "随便聊聊"], projects) is None

    many_projects = {f"project-{index}": f"项目{index}" for index in range(7)}
    assert match_project_choices(list(many_projects.values()), many_projects) is None


def test_card_callback_parses_real_nested_wecom_shape() -> None:
    payload = {
        "cmd": "aibot_event_callback",
        "headers": {"req_id": "req-card"},
        "body": {
            "from": {"userid": "user-1"},
            "chatid": "chat-1",
            "chattype": "group",
            "event": {
                "eventtype": "template_card_event",
                "template_card_event": {
                    "event_key": "aijd-demo",
                    "task_id": "clarify-1",
                },
            },
        },
    }

    assert parse_card_event(payload) == CardEvent(
        request_id="req-card",
        clarify_id="clarify-1",
        project_id="aijd-demo",
        user_id="user-1",
        chat_id="chat-1",
        chat_type="group",
    )


def test_invalid_or_unrelated_callback_is_ignored() -> None:
    assert parse_card_event({"cmd": "aibot_event_callback", "body": {}}) is None
    assert parse_card_event({"cmd": "aibot_callback", "body": {}}) is None
