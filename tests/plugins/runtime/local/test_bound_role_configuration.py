import os
from pathlib import Path

import pytest

from kora.plugins.runtime.local.bound_role_configuration import (
    CONFIG_NAME,
    MAX_CONFIG_BYTES,
    BoundRoleConfiguration,
    BoundRoleConfigurationError,
    RoleConfigurationFailure,
    RoleDirectories,
)
from tests.plugins.runtime.local.frames_support import (
    ORIGIN_NS,
    StepClock,
    TickingClock,
)

CONTENT = b'model = "offline-model"\n'
CUTOFF = ORIGIN_NS + 100


def role_directories(root: Path) -> RoleDirectories:
    return RoleDirectories(root / "home", root / "codex", root / "tmp")


def create_directories(paths: RoleDirectories) -> None:
    for path in paths.paths:
        path.mkdir(mode=0o700)


def create_private_config(path: Path, content: bytes) -> None:
    path.touch(mode=0o600, exist_ok=False)
    path.write_bytes(content)


def test_configuration_releases_partial_placement_capture(tmp_path: Path) -> None:
    paths = role_directories(tmp_path.resolve())
    paths.home.mkdir(mode=0o700)
    paths.codex_home.symlink_to(paths.home, target_is_directory=True)
    baseline = set(os.listdir("/dev/fd"))
    role = BoundRoleConfiguration(paths, CONTENT)

    try:
        with pytest.raises(BoundRoleConfigurationError) as refused:
            role.prepare(CUTOFF, StepClock())
        captured = set(os.listdir("/dev/fd"))
    finally:
        role.close()
        role.close()

    assert refused.value.reason is RoleConfigurationFailure.PLACEMENT
    assert len(captured - baseline) == 1
    assert set(os.listdir("/dev/fd")) == baseline


def test_configuration_rejects_oversized_expected_bytes(tmp_path: Path) -> None:
    paths = role_directories(tmp_path.resolve())
    oversized = b"x" * (MAX_CONFIG_BYTES + 1)

    with pytest.raises(BoundRoleConfigurationError) as refused:
        BoundRoleConfiguration(paths, oversized)

    assert refused.value.reason is RoleConfigurationFailure.BYTES


@pytest.mark.parametrize(
    ("change", "failure"),
    [
        ("bytes", RoleConfigurationFailure.BYTES),
        ("longer", RoleConfigurationFailure.BYTES),
        ("replacement", RoleConfigurationFailure.FILE),
        ("mode", RoleConfigurationFailure.FILE),
        ("hardlink", RoleConfigurationFailure.FILE),
        ("symlink", RoleConfigurationFailure.FILE),
        ("placement", RoleConfigurationFailure.PLACEMENT),
    ],
)
def test_configuration_distinguishes_bytes_identity_and_private_placements(
    tmp_path: Path, change: str, failure: RoleConfigurationFailure
) -> None:
    paths = role_directories(tmp_path.resolve())
    create_directories(paths)
    config = paths.codex_home / CONFIG_NAME
    baseline = set(os.listdir("/dev/fd"))
    role = BoundRoleConfiguration(paths, CONTENT)
    clock = StepClock()
    try:
        role.prepare(CUTOFF, clock)
        role.verify(paths, CUTOFF, clock)
        owned = set(os.listdir("/dev/fd"))
        if change == "bytes":
            config.write_bytes(b"x" * len(CONTENT))
        elif change == "longer":
            config.write_bytes(CONTENT + b"x" * MAX_CONFIG_BYTES)
        elif change == "replacement":
            config.unlink()
            create_private_config(config, CONTENT)
        elif change == "mode":
            config.chmod(0o644)
        elif change == "hardlink":
            os.link(config, paths.home / CONFIG_NAME)
        elif change == "symlink":
            config.rename(paths.home / CONFIG_NAME)
            config.symlink_to(paths.home / CONFIG_NAME)
        else:
            paths.codex_home.rename(paths.codex_home.with_name("original"))
            paths.codex_home.mkdir(mode=0o700)
            create_private_config(config, CONTENT)

        with pytest.raises(BoundRoleConfigurationError) as refused:
            role.verify(paths, CUTOFF, clock)
        retained = set(os.listdir("/dev/fd"))
    finally:
        role.close()

    assert refused.value.reason is failure
    assert retained == owned
    assert len(owned - baseline) == 4
    assert set(os.listdir("/dev/fd")) == baseline


def test_configuration_checks_cutoff_after_comparison_descriptor_is_closed(
    tmp_path: Path,
) -> None:
    paths = role_directories(tmp_path.resolve())
    create_directories(paths)
    baseline = set(os.listdir("/dev/fd"))
    role = BoundRoleConfiguration(paths, CONTENT)
    clock = TickingClock(1)
    try:
        role.prepare(CUTOFF, StepClock())
        owned = set(os.listdir("/dev/fd"))

        with pytest.raises(BoundRoleConfigurationError) as expired:
            role.verify(paths, ORIGIN_NS + 8, clock)
        retained = set(os.listdir("/dev/fd"))
    finally:
        role.close()

    assert expired.value.reason is RoleConfigurationFailure.DEADLINE
    assert clock.offset_ns == 8
    assert retained == owned
    assert len(owned - baseline) == 4
    assert set(os.listdir("/dev/fd")) == baseline
