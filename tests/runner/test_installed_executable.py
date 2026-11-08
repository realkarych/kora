import hashlib
import os
from pathlib import Path

import pytest

from kora.protocols.time import SystemClock
from kora.runner.installed_executable import (
    ExecutableFailure,
    ExecutablePin,
    ExecutableVerificationError,
    check_executable_entry,
    verify_executable_contents,
    verify_installed_executable,
)
from tests.plugins.runtime.local.frames_support import ORIGIN_NS, TickingClock

PROTECTED_EXECUTABLE = Path("/usr/bin/true")
FILE_BYTES = b"selected executable content"


def pin_for(data: bytes) -> ExecutablePin:
    return ExecutablePin(len(data), hashlib.sha256(data).hexdigest())


def test_checked_installed_file_releases_all_its_descriptors() -> None:
    path = PROTECTED_EXECUTABLE
    data = path.read_bytes()
    entry = path.stat()
    pin = pin_for(data)
    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    baseline = set(os.listdir("/dev/fd"))

    verified = verify_installed_executable(path, pin, deadline, clock)
    with pytest.raises(ExecutableVerificationError) as wrong:
        verify_installed_executable(
            path, ExecutablePin(pin.size, "0" * 64), deadline, clock
        )

    assert verified.path == path
    assert (verified.device, verified.inode) == (entry.st_dev, entry.st_ino)
    assert wrong.value.reason is ExecutableFailure.DIGEST
    assert set(os.listdir("/dev/fd")) == baseline


def test_checked_install_closes_descriptors_at_cutoff() -> None:
    path = PROTECTED_EXECUTABLE
    pin = pin_for(path.read_bytes())
    clock = TickingClock(1)
    deadline = ORIGIN_NS + 7
    baseline = set(os.listdir("/dev/fd"))

    with pytest.raises(ExecutableVerificationError) as expired:
        verify_installed_executable(path, pin, deadline, clock)

    assert expired.value.reason is ExecutableFailure.DEADLINE
    assert set(os.listdir("/dev/fd")) == baseline


def test_checked_install_requires_actual_execute_access() -> None:
    path = Path("/etc/passwd").resolve()
    pin = ExecutablePin(1, "0" * 64)
    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000

    with pytest.raises(ExecutableVerificationError) as refused:
        verify_installed_executable(path, pin, deadline, clock)

    assert not os.access(path, os.X_OK, effective_ids=True)
    assert refused.value.reason is ExecutableFailure.PROTECTION


def test_checked_install_rejects_replaceable_ancestry(tmp_path: Path) -> None:
    parent = tmp_path.resolve() / "candidate"
    parent.mkdir()
    executable = parent / "true"
    staged = parent / "staged"
    staged.write_bytes(FILE_BYTES)
    staged.chmod(0o755)
    staged.rename(executable)
    clock = SystemClock()
    baseline = set(os.listdir("/dev/fd"))

    with pytest.raises(ExecutableVerificationError) as writable:
        verify_installed_executable(
            executable, pin_for(FILE_BYTES), clock.monotonic_ns() + 5_000_000_000, clock
        )

    assert writable.value.reason is ExecutableFailure.PROTECTION
    assert set(os.listdir("/dev/fd")) == baseline


def test_file_check_refuses_writable_file_and_special_objects(tmp_path: Path) -> None:
    executable = tmp_path / "writable"
    executable.write_bytes(FILE_BYTES)
    executable.chmod(0o777)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    alias = tmp_path / "alias"
    alias.symlink_to(PROTECTED_EXECUTABLE)

    with pytest.raises(ExecutableVerificationError) as writable:
        check_executable_entry(executable.stat())
    with pytest.raises(ExecutableVerificationError) as special:
        check_executable_entry(fifo.stat())
    with pytest.raises(ExecutableVerificationError) as symbolic:
        check_executable_entry(alias.lstat())

    assert writable.value.reason is ExecutableFailure.PROTECTION
    assert special.value.reason is ExecutableFailure.LOCATION
    assert symbolic.value.reason is ExecutableFailure.LOCATION


def test_file_scan_checks_identity_size_and_final_deadline(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.write_bytes(FILE_BYTES)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(FILE_BYTES)
    entry = selected.stat()
    pin = pin_for(FILE_BYTES)
    clock = TickingClock(1)
    deadline = ORIGIN_NS + 4

    with selected.open("rb") as original, replacement.open("rb") as other:
        with pytest.raises(ExecutableVerificationError) as changed:
            verify_executable_contents(
                other.fileno(), entry, pin, ORIGIN_NS + 5, TickingClock(1)
            )
        with pytest.raises(ExecutableVerificationError) as wrong_size:
            verify_executable_contents(
                original.fileno(),
                entry,
                pin_for(FILE_BYTES + b"!"),
                ORIGIN_NS + 5,
                TickingClock(1),
            )
        with pytest.raises(ExecutableVerificationError) as expired:
            verify_executable_contents(original.fileno(), entry, pin, deadline, clock)

    assert changed.value.reason is ExecutableFailure.IDENTITY
    assert wrong_size.value.reason is ExecutableFailure.SIZE
    assert expired.value.reason is ExecutableFailure.DEADLINE
    assert clock.offset_ns == 4
