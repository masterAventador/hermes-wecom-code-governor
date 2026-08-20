from __future__ import annotations

from pathlib import Path

_HOME_SECRET_DIRS = (
    ".ssh",
    ".aws",
    ".codex",
    ".hermes",
    "Library/Keychains",
    # 签名/公证材料：p12、p12 密码、公证 p8 及凭据总包，沙箱任务一律拒读。
    ".vpp-signing",
    ".appstoreconnect",
    ".at-tools-credentials",
)


def _sbpl(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')


def build_seatbelt_profile(
    write_paths: tuple[Path, ...],
    denied_read_paths: tuple[Path, ...] = (),
) -> str:
    """默认放行 + 定点收紧的 seatbelt profile，供校验沙箱和 GUI 任务执行器共用。

    网络：外部全部拒绝、仅允许本机回环（校验测试与 QA 常需临时本地端口）。
    写入：默认拒绝，仅放行指定目录和 /dev/null。
    读取：默认放行（GUI/系统服务需要），但显式拒绝密钥目录。
    注意：只要 profile 含任何文件写限制，macOS Security 框架就会禁用钥匙串
    身份枚举——需要 codesign 签名的命令不能进本沙箱，走 TrustedExecutor。
    """
    real_home = Path.home().resolve()
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        '(allow network-bind network-inbound (local ip "*:*"))',
        '(allow network-outbound (remote ip "localhost:*"))',
        "(deny file-write*)",
        '(allow file-write* (literal "/dev/null"))',
    ]
    lines.extend(f'(allow file-write* (subpath "{_sbpl(path)}"))' for path in write_paths)
    secret_paths = tuple(real_home / relative for relative in _HOME_SECRET_DIRS)
    lines.extend(
        f'(deny file-read* (subpath "{_sbpl(path)}"))'
        for path in (*secret_paths, *denied_read_paths)
    )
    return "\n".join(lines)
