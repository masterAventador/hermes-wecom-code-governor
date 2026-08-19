from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .execution import TIMEOUT_EXIT_CODE
from .policy import RemoteAction

# ssh 连接建立阶段的等待上限；命令整体超时由 RemoteAction.timeout_seconds 控制。
_MAX_SSH_CONNECT_TIMEOUT = 20


@dataclass(frozen=True)
class RemoteRunResult:
    exit_code: int
    stdout: str
    stderr: str


class SshRemoteRunner:
    """执行预登记远程动作：ssh 到固定主机运行固定命令。

    host 与命令 argv 完全来自配置，模型只能按名称触发、无法拼接。安全约束全部
    显式钉在 argv 里，不依赖运行机的 ~/.ssh/config：BatchMode 禁止交互式认证，
    StrictHostKeyChecking=yes 拒绝未知/变更的主机密钥。环境被清成仅剩 PATH，
    机器人密钥与企微凭据不进子进程（同时意味着不支持 ssh-agent 转发认证，
    只走本机已配置的免密私钥）。ssh 会把 argv 用空格拼成单串交远端 shell 解析，
    因此登记命令的每个 token 不应含空格或 shell 元字符。
    """

    @staticmethod
    def build_command(action: RemoteAction) -> tuple[str, ...]:
        connect_timeout = min(action.timeout_seconds, _MAX_SSH_CONNECT_TIMEOUT)
        return (
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={connect_timeout}",
            action.host,
            "--",
            *action.argv,
        )

    def run(self, action: RemoteAction) -> RemoteRunResult:
        try:
            completed = subprocess.run(
                self.build_command(action),
                capture_output=True,
                text=True,
                check=False,
                timeout=action.timeout_seconds,
                env={"PATH": "/usr/bin:/bin"},
            )
        except subprocess.TimeoutExpired:
            message = f"remote action timed out after {action.timeout_seconds}s"
            return RemoteRunResult(TIMEOUT_EXIT_CODE, "", message)
        return RemoteRunResult(completed.returncode, completed.stdout, completed.stderr)
