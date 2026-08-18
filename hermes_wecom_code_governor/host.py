from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import version

SUPPORTED_HERMES_VERSION = "0.20.2"
MAX_EXCLUSIVE_HERMES_VERSION = "0.21.0"


def _release_tuple(value: str) -> tuple[int, int, int] | None:
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(part) for part in parts)
    except ValueError:
        return None
    return major, minor, patch


def require_supported_hermes_version(
    version_getter: Callable[[str], str] = version,
) -> str:
    actual = version_getter("hermes-agent")
    actual_release = _release_tuple(actual)
    minimum_release = _release_tuple(SUPPORTED_HERMES_VERSION)
    maximum_release = _release_tuple(MAX_EXCLUSIVE_HERMES_VERSION)
    if (
        actual_release is None
        or minimum_release is None
        or maximum_release is None
        or not minimum_release <= actual_release < maximum_release
    ):
        raise RuntimeError(
            "hermes-wecom-code-governor requires Hermes "
            f">={SUPPORTED_HERMES_VERSION},<{MAX_EXCLUSIVE_HERMES_VERSION}, "
            f"but found {actual}"
        )
    return actual
