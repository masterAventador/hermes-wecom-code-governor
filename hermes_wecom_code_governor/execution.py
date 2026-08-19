from __future__ import annotations

# 命令在超时被强杀时统一返回的退出码；worktree 校验、任务执行、远程动作共用。
TIMEOUT_EXIT_CODE = 124
# 回传给外层模型的命令输出上限，超过后只保留尾部，避免淹没上下文并转发进群。
MAX_OUTPUT_CHARS = 12_000


def combine_output(stdout: str, stderr: str) -> str:
    """合并 stdout/stderr（各自去空、按出现拼接）并把过长输出截到尾部。"""
    output = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
    if len(output) <= MAX_OUTPUT_CHARS:
        return output
    omitted = len(output) - MAX_OUTPUT_CHARS
    return f"[前面 {omitted} 个字符已省略]\n{output[-MAX_OUTPUT_CHARS:]}"
