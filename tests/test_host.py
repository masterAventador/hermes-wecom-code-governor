import pytest

from hermes_wecom_code_governor.host import (
    SUPPORTED_HERMES_VERSION,
    require_supported_hermes_version,
)


def test_accepts_the_pinned_hermes_release() -> None:
    assert require_supported_hermes_version(lambda _: SUPPORTED_HERMES_VERSION) == (
        SUPPORTED_HERMES_VERSION
    )


def test_accepts_a_compatible_hermes_patch_release() -> None:
    assert require_supported_hermes_version(lambda _: "0.20.3") == "0.20.3"


def test_rejects_an_unverified_hermes_release() -> None:
    with pytest.raises(RuntimeError, match="requires Hermes"):
        require_supported_hermes_version(lambda _: "0.21.0")


@pytest.mark.parametrize("release", ["0.20.1", "0.20.3rc1", "unknown"])
def test_rejects_a_release_outside_the_compatible_patch_range(release: str) -> None:
    with pytest.raises(RuntimeError, match="requires Hermes"):
        require_supported_hermes_version(lambda _: release)
