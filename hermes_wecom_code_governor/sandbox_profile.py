from __future__ import annotations

from pathlib import Path

_HOME_SECRET_DIRS = (".ssh", ".aws", ".codex", ".hermes", "Library/Keychains")


def _sbpl(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')


def build_seatbelt_profile(
    write_paths: tuple[Path, ...],
    denied_read_paths: tuple[Path, ...] = (),
    *,
    allow_outbound_network: bool = False,
) -> str:
    """默认放行 + 定点收紧的 seatbelt profile，供校验沙箱和 GUI/出网任务执行器共用。

    网络：默认外部全部拒绝、仅允许本机回环；allow_outbound_network=True 时不禁网
    （签名公证等登记动作需要访问外部服务），写入与密钥拒读约束保持不变。
    写入：默认拒绝，仅放行指定目录和 /dev/null。
    读取：默认放行（GUI/系统服务需要），但显式拒绝密钥目录。
    """
    real_home = Path.home().resolve()
    lines = ["(version 1)", "(allow default)"]
    if not allow_outbound_network:
        lines += [
            "(deny network*)",
            # 校验里的测试和 QA 运行常需要本机回环端口（临时 broker、本地服务），
            # 只放行本地监听和到 localhost 的出站，外部网络仍然全部拒绝。
            '(allow network-bind network-inbound (local ip "*:*"))',
            '(allow network-outbound (remote ip "localhost:*"))',
        ]
    lines += [
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
