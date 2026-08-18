from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    message: str = ""


@dataclass(frozen=True)
class ToolScope:
    project_path: Path
    worktree_path: Path | None = None
    allowed_command_prefixes: tuple[tuple[str, ...], ...] = ()

    @property
    def execution_root(self) -> Path:
        return self.worktree_path or self.project_path


_FILE_MUTATION_TOOLS = frozenset({"write_file", "patch"})
_FILE_READ_TOOLS = frozenset({"read_file", "search_files"})
_V4A_FILE_HEADER = re.compile(
    r"^(?P<prefix>\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*)(?P<path>.+)$",
    re.MULTILINE,
)
_V4A_MOVE_HEADER = re.compile(
    r"^(?P<prefix>\*\*\*\s*Move\s+File:\s*)(?P<src>.+?)\s*->\s*(?P<dst>.+)$",
    re.MULTILINE,
)
_COMPOUND_SHELL = re.compile(r"(?:&&|\|\||[;<>`\n]|\$\()")
_READ_ONLY_COMMANDS = frozenset({"pwd", "rg", "ls"})
_READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {"status", "diff", "log", "show", "rev-parse", "ls-files", "grep", "blame", "describe"}
)
_BLOCKED_EXECUTABLES = frozenset(
    {
        "rm",
        "rmdir",
        "unlink",
        "mv",
        "truncate",
        "dd",
        "mkfs",
        "chmod",
        "chown",
        "sudo",
        "kill",
        "killall",
        "pkill",
        "osascript",
        "bash",
        "sh",
        "zsh",
        "fish",
    }
)
_BLOCKED_GIT_SUBCOMMANDS = frozenset(
    {
        "push",
        "merge",
        "reset",
        "clean",
        "checkout",
        "switch",
        "commit",
        "add",
        "restore",
        "rebase",
        "cherry-pick",
        "stash",
        "worktree",
        "branch",
    }
)
_INLINE_CODE_FLAGS = frozenset({"-c", "-e", "--eval"})


def _allow() -> GuardDecision:
    return GuardDecision(True)


def _block(message: str) -> GuardDecision:
    return GuardDecision(False, message)


def _is_within(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _resolve_tool_path(value: object, root: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def _evaluate_file_tool(tool_name: str, args: dict, scope: ToolScope) -> GuardDecision:
    if tool_name in _FILE_MUTATION_TOOLS and scope.worktree_path is None:
        return _block("blocked: file mutation requires an active worktree")
    root = scope.worktree_path if tool_name in _FILE_MUTATION_TOOLS else scope.project_path
    if root is None:
        return _block("blocked: no authorized project root")

    raw_paths: list[object]
    if tool_name == "patch" and args.get("mode") == "patch":
        patch = args.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            return _block("blocked: patch content cannot be validated")
        raw_paths = [match.group("path").strip() for match in _V4A_FILE_HEADER.finditer(patch)]
        for match in _V4A_MOVE_HEADER.finditer(patch):
            raw_paths.extend((match.group("src").strip(), match.group("dst").strip()))
        if not raw_paths:
            return _block("blocked: patch contains no validated file targets")
    else:
        raw_paths = [args.get("path", ".") if tool_name == "search_files" else args.get("path")]

    for raw_path in raw_paths:
        path = _resolve_tool_path(raw_path, root)
        if path is None:
            return _block("blocked: file path cannot be validated")
        if not _is_within(path, root):
            return _block(f"blocked: file path is outside authorized root {root}")
    return _allow()


def _absolute_path_text(value: str, root: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return str(path.resolve())


def _rewrite_v4a_headers(patch: str, root: Path) -> str:
    def rewrite_file(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}{_absolute_path_text(match.group('path').strip(), root)}"

    def rewrite_move(match: re.Match[str]) -> str:
        src = _absolute_path_text(match.group("src").strip(), root)
        dst = _absolute_path_text(match.group("dst").strip(), root)
        return f"{match.group('prefix')}{src} -> {dst}"

    return _V4A_MOVE_HEADER.sub(rewrite_move, _V4A_FILE_HEADER.sub(rewrite_file, patch))


def rewrite_tool_args(tool_name: str, args: dict, scope: ToolScope) -> dict:
    """Pin relative paths to the checkout that the guard actually authorized."""
    rewritten = dict(args)
    if tool_name in _FILE_MUTATION_TOOLS:
        if scope.worktree_path is None:
            return rewritten
        root = scope.worktree_path
        if tool_name == "patch" and rewritten.get("mode") == "patch":
            patch = rewritten.get("patch")
            if isinstance(patch, str):
                rewritten["patch"] = _rewrite_v4a_headers(patch, root)
        elif isinstance(rewritten.get("path"), str):
            rewritten["path"] = _absolute_path_text(rewritten["path"], root)
    elif tool_name in _FILE_READ_TOOLS:
        raw_path = rewritten.get("path", ".")
        if isinstance(raw_path, str):
            rewritten["path"] = _absolute_path_text(raw_path, scope.project_path)
    elif tool_name == "terminal":
        raw_workdir = rewritten.get("workdir", str(scope.execution_root))
        if isinstance(raw_workdir, str):
            rewritten["workdir"] = _absolute_path_text(raw_workdir, scope.execution_root)
    return rewritten


def _token_path(token: str) -> str | None:
    candidate = token.split("=", 1)[1] if "=" in token else token
    if candidate.startswith(("/", "~/")):
        return candidate
    return None


def _validate_command_paths(argv: list[str], execution_root: Path) -> GuardDecision | None:
    for token in argv[1:]:
        candidate = token.split("=", 1)[1] if "=" in token else token
        if ".." in Path(candidate).parts:
            return _block("blocked: relative path traversal is not allowed")
        raw_path = _token_path(token)
        if raw_path is None:
            continue
        path = Path(raw_path).expanduser().resolve()
        if not _is_within(path, execution_root):
            return _block(f"blocked: terminal path is outside execution root {execution_root}")
    return None


def _matches_prefix(argv: list[str], prefix: tuple[str, ...]) -> bool:
    return len(argv) >= len(prefix) and tuple(argv[: len(prefix)]) == prefix


def _evaluate_terminal(args: dict, scope: ToolScope) -> GuardDecision:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return _block("blocked: terminal command is empty")
    if (
        args.get("background")
        or args.get("pty")
        or args.get("notify_on_complete")
        or args.get("watch_patterns")
    ):
        return _block("blocked: background and interactive terminal execution is not allowed")
    if _COMPOUND_SHELL.search(command):
        return _block("blocked: compound shell syntax is not allowed")
    try:
        argv = shlex.split(command)
    except ValueError:
        return _block("blocked: terminal command cannot be parsed safely")
    if not argv:
        return _block("blocked: terminal command is empty")

    executable = Path(argv[0]).name
    if executable in _BLOCKED_EXECUTABLES:
        return _block(f"blocked: executable {executable!r} is forbidden")
    if executable in {"python", "python3", "node", "ruby", "perl"} and any(
        flag in argv[1:] for flag in _INLINE_CODE_FLAGS
    ):
        return _block("blocked: inline executable code is not allowed")
    if executable == "git" and len(argv) > 1 and argv[1] in _BLOCKED_GIT_SUBCOMMANDS:
        return _block(f"blocked: git {argv[1]} is managed by the worktree lifecycle")
    if executable == "rg" and any(flag in {"--follow", "-L"} for flag in argv[1:]):
        return _block("blocked: following symlinks is not allowed")

    root = scope.execution_root
    workdir = _resolve_tool_path(args.get("workdir", str(root)), root)
    if workdir is None or not _is_within(workdir, root):
        return _block(f"blocked: terminal workdir is outside execution root {root}")
    path_decision = _validate_command_paths(argv, root)
    if path_decision is not None:
        return path_decision

    if executable in _READ_ONLY_COMMANDS:
        return _allow()
    if executable == "git" and len(argv) > 1 and argv[1] in _READ_ONLY_GIT_SUBCOMMANDS:
        return _allow()
    if scope.worktree_path is not None and any(
        _matches_prefix(argv, prefix) for prefix in scope.allowed_command_prefixes
    ):
        return _block(
            "blocked: configured validation commands are managed by governor_complete_task"
        )
    return _block(f"blocked: command {command!r} is not allowed by project policy")


def evaluate_tool_call(tool_name: str, args: dict, scope: ToolScope | None) -> GuardDecision:
    if tool_name in _FILE_MUTATION_TOOLS | _FILE_READ_TOOLS:
        if scope is None:
            return _block("blocked: no authorized project is selected")
        return _evaluate_file_tool(tool_name, args, scope)
    if tool_name == "terminal":
        if scope is None:
            return _block("blocked: no authorized project is selected")
        return _evaluate_terminal(args, scope)
    return _allow()
