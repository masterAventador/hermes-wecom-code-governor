from __future__ import annotations

import subprocess

import pytest

from hermes_wecom_code_governor.policy import RemoteAction
from hermes_wecom_code_governor.remote import SshRemoteRunner


def action(**changes: object) -> RemoteAction:
    values: dict[str, object] = {
        "name": "生成激活码",
        "host": "root@license.example",
        "argv": ("node", "/opt/issue.mjs"),
        "timeout_seconds": 30,
    }
    values.update(changes)
    return RemoteAction(**values)  # type: ignore[arg-type]


def test_build_command_pins_batch_mode_strict_host_key_and_separates_argv() -> None:
    command = SshRemoteRunner.build_command(action())

    assert command == (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=20",
        "root@license.example",
        "--",
        "node",
        "/opt/issue.mjs",
    )


def test_build_command_connect_timeout_never_exceeds_the_action_timeout() -> None:
    command = SshRemoteRunner.build_command(action(timeout_seconds=8))

    assert "ConnectTimeout=8" in command


def test_run_maps_a_timeout_to_the_shared_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=30)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = SshRemoteRunner().run(action())

    assert result.exit_code == 124
    assert result.stdout == ""
    assert "timed out" in result.stderr


def test_run_passes_the_built_command_with_a_minimal_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        assert kwargs.get("capture_output") is True
        assert "shell" not in kwargs
        return subprocess.CompletedProcess(list(command), 0, "VPP-CODE\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = SshRemoteRunner().run(action())

    assert result.exit_code == 0
    assert result.stdout == "VPP-CODE\n"
    assert captured["command"] == SshRemoteRunner.build_command(action())
    assert captured["env"] == {"PATH": "/usr/bin:/bin"}
