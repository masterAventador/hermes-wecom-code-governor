from pathlib import Path

from hermes_wecom_code_governor.guard import (
    GuardDecision,
    ToolScope,
    evaluate_tool_call,
    rewrite_tool_args,
)

PROJECT = Path("/workspace/sourceCode/vpp-digital-twin")
WORKTREE = Path("/runtime/worktrees/vpp/task-1")


def _scope(*, active: bool = False) -> ToolScope:
    return ToolScope(
        project_path=PROJECT,
        worktree_path=WORKTREE if active else None,
        allowed_command_prefixes=(("npm", "test"), ("npm", "run", "lint")),
    )


def assert_blocked(decision: GuardDecision, text: str) -> None:
    assert not decision.allowed
    assert text in decision.message


def test_file_mutation_requires_an_active_worktree() -> None:
    decision = evaluate_tool_call(
        "write_file",
        {"path": str(PROJECT / "README.md"), "content": "changed"},
        _scope(),
    )

    assert_blocked(decision, "worktree")


def test_file_mutation_is_limited_to_active_worktree() -> None:
    scope = _scope(active=True)

    assert evaluate_tool_call(
        "write_file",
        {"path": str(WORKTREE / "README.md"), "content": "changed"},
        scope,
    ).allowed
    assert_blocked(
        evaluate_tool_call(
            "patch",
            {"mode": "replace", "path": str(PROJECT / "README.md")},
            scope,
        ),
        "outside",
    )
    assert_blocked(
        evaluate_tool_call(
            "write_file",
            {"path": "/Users/aventador/.ssh/config", "content": "Host *"},
            scope,
        ),
        "outside",
    )


def test_relative_file_path_is_resolved_inside_current_execution_root() -> None:
    assert evaluate_tool_call(
        "patch",
        {"mode": "replace", "path": "src/main.ts"},
        _scope(active=True),
    ).allowed


def test_project_question_can_use_read_only_tools_without_worktree() -> None:
    scope = _scope()

    assert evaluate_tool_call("read_file", {"path": "README.md"}, scope).allowed
    assert evaluate_tool_call("terminal", {"command": "rg TODO src"}, scope).allowed
    assert evaluate_tool_call("terminal", {"command": "git status --short"}, scope).allowed


def test_read_is_limited_to_selected_project() -> None:
    decision = evaluate_tool_call("read_file", {"path": "/workspace/other/secrets.txt"}, _scope())

    assert_blocked(decision, "outside")


def test_destructive_and_repository_publishing_commands_are_always_blocked() -> None:
    scope = _scope(active=True)

    for command in (
        "rm -rf .",
        "git push origin dev",
        "git merge feature/x",
        "git reset --hard HEAD~1",
        "sudo chmod -R 777 /workspace",
    ):
        assert_blocked(evaluate_tool_call("terminal", {"command": command}, scope), "blocked")


def test_shell_compound_syntax_cannot_hide_a_second_command() -> None:
    scope = _scope(active=True)

    for command in (
        "npm test && rm -rf /Users/aventador/sourceCode",
        "rg TODO src; git push",
        "npm test > /tmp/result.txt",
        'node -e \'require("fs").rmSync("/tmp/x")\'',
    ):
        assert_blocked(evaluate_tool_call("terminal", {"command": command}, scope), "blocked")


def test_only_configured_project_commands_run_in_worktree() -> None:
    scope = _scope(active=True)

    # Validation commands run only through WorktreeManager's sandboxed
    # completion path. Letting the model invoke repository scripts directly
    # would let a malicious package.json escape the lifecycle guard.
    assert_blocked(
        evaluate_tool_call("terminal", {"command": "npm test -- --runInBand"}, scope),
        "managed",
    )
    assert_blocked(
        evaluate_tool_call("terminal", {"command": "npm run lint"}, scope),
        "managed",
    )
    assert_blocked(
        evaluate_tool_call("terminal", {"command": "npm run deploy"}, scope),
        "not allowed",
    )


def test_terminal_workdir_cannot_escape_execution_root() -> None:
    decision = evaluate_tool_call(
        "terminal",
        {"command": "rg TODO", "workdir": "/Users/aventador/sourceCode"},
        _scope(active=True),
    )

    assert_blocked(decision, "outside")


def test_terminal_rejects_relative_traversal_and_background_execution() -> None:
    scope = _scope(active=True)

    assert_blocked(
        evaluate_tool_call("terminal", {"command": "rg password ../../"}, scope),
        "traversal",
    )
    assert_blocked(
        evaluate_tool_call(
            "terminal",
            {"command": "rg TODO", "background": True, "notify_on_complete": True},
            scope,
        ),
        "background",
    )


def test_search_is_limited_to_selected_project() -> None:
    scope = _scope()

    assert evaluate_tool_call("search_files", {"pattern": "TODO", "path": "src"}, scope).allowed
    assert_blocked(
        evaluate_tool_call(
            "search_files",
            {"pattern": "secret", "path": "/Users/aventador/.ssh"},
            scope,
        ),
        "outside",
    )


def test_v4a_patch_validates_every_target_inside_worktree() -> None:
    scope = _scope(active=True)

    assert evaluate_tool_call(
        "patch",
        {
            "mode": "patch",
            "patch": "*** Begin Patch\n*** Update File: src/main.ts\n*** End Patch",
        },
        scope,
    ).allowed
    assert_blocked(
        evaluate_tool_call(
            "patch",
            {
                "mode": "patch",
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: src/main.ts\n"
                    "*** Delete File: /Users/aventador/.ssh/config\n"
                    "*** End Patch"
                ),
            },
            scope,
        ),
        "outside",
    )


def test_relative_tool_paths_are_rewritten_to_the_authorized_checkout() -> None:
    readonly = rewrite_tool_args("read_file", {"path": "README.md"}, _scope())
    mutation = rewrite_tool_args(
        "write_file",
        {"path": "src/main.ts", "content": "changed"},
        _scope(active=True),
    )
    search = rewrite_tool_args("search_files", {"pattern": "TODO"}, _scope())
    terminal = rewrite_tool_args("terminal", {"command": "git status"}, _scope(active=True))

    assert readonly["path"] == str(PROJECT / "README.md")
    assert mutation["path"] == str(WORKTREE / "src/main.ts")
    assert search["path"] == str(PROJECT)
    assert terminal["workdir"] == str(WORKTREE)


def test_v4a_patch_headers_are_rewritten_to_absolute_worktree_paths() -> None:
    rewritten = rewrite_tool_args(
        "patch",
        {
            "mode": "patch",
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: src/main.ts\n"
                "*** Move File: old.txt -> archive/old.txt\n"
                "*** End Patch"
            ),
        },
        _scope(active=True),
    )

    assert f"*** Update File: {WORKTREE / 'src/main.ts'}" in rewritten["patch"]
    assert (
        f"*** Move File: {WORKTREE / 'old.txt'} -> {WORKTREE / 'archive/old.txt'}"
        in rewritten["patch"]
    )
