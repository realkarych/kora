import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from kora.db.custody_models import CustodyUnavailable
from kora.db.custody_native import (
    DESCRIPTORS,
    FILE_NAMES,
    CustodyLatch,
    CustodyLifetimeLease,
    CustodyNativeDirectory,
    CustodyRecoveryLease,
)
from kora.standalone.custody import NativeCustodyPlatform

FORK_OWNERSHIP = """
import errno, os, sys
from pathlib import Path
from kora.db.custody_models import CustodyUnavailable
from kora.db.custody_native import DESCRIPTORS, CustodyNativeDirectory
from kora.standalone.custody import NativeCustodyPlatform
root = CustodyNativeDirectory(
    Path(sys.argv[1]), NativeCustodyPlatform(), provision=True
)
try:
    with root.acquire("lifetime.lock", exclusive=False):
        descriptors = tuple(handle.descriptor for handle in DESCRIPTORS)
        child = os.fork()
        if child == 0:
            assert not DESCRIPTORS
            for descriptor in descriptors:
                try:
                    os.fstat(descriptor)
                except OSError as error:
                    assert error.errno == errno.EBADF
                else:
                    raise AssertionError("inherited descriptor remained open")
            try:
                root.require_current()
            except CustodyUnavailable:
                pass
            else:
                raise AssertionError("copied owner remained usable")
            os._exit(0)
        observed, status = os.waitpid(child, 0)
        assert observed == child and status == 0
        root.require_current()
        other = CustodyNativeDirectory(
            root.path, NativeCustodyPlatform(), provision=False
        )
        try:
            try:
                with other.acquire("lifetime.lock"):
                    raise AssertionError("fork child unlocked parent ownership")
            except BlockingIOError:
                pass
        finally:
            other.close()
finally:
    root.close()
assert not DESCRIPTORS
sys.stdout.write("closed\\n")
"""


def journal_path(tmp_path: Path) -> Path:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    return parent / "authority"


def test_native_custody_locks_exclude_competitors_and_close_descriptors(
    tmp_path: Path,
) -> None:
    baseline = set(DESCRIPTORS)
    path = journal_path(tmp_path)
    first = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    second = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=False)
    before_contention = set(DESCRIPTORS)
    anchors = dict(first.file_anchors)

    try:
        with first.acquire("lifetime.lock", exclusive=False):
            with second.acquire("lifetime.lock", exclusive=False):
                with pytest.raises(BlockingIOError), second.acquire("lifetime.lock"):
                    pytest.fail("exclusive lifetime admitted while shared owners live")
            with first.acquire("commit.lock"):
                with pytest.raises(BlockingIOError), second.acquire("commit.lock"):
                    pytest.fail("two commit owners admitted")
                with second.acquire("recovery.lock"):
                    second.require_current()
        after_contention = set(DESCRIPTORS)
        with second.acquire("lifetime.lock"), second.acquire("commit.lock"):
            second.require_current()
        retained = dict(second.file_anchors)
    finally:
        second.close()
        first.close()

    assert after_contention == before_contention
    assert retained == anchors
    assert (path / "recovery.lock").read_bytes() == b"\x5a"
    assert all(
        (path / name).stat().st_size == 0
        for name in FILE_NAMES
        if name != "recovery.lock"
    )
    assert set(DESCRIPTORS) == baseline


@pytest.mark.parametrize(
    "entry",
    [
        "authority.sqlite3",
        "commit.lock",
        "lifetime.lock",
        "recovery.lock",
        "journal-order.lock",
    ],
)
@pytest.mark.parametrize("mutation", ["replacement", "symlink", "hardlink", "mode"])
def test_native_custody_refuses_changed_or_shared_files(
    tmp_path: Path, entry: str, mutation: str
) -> None:
    baseline = set(DESCRIPTORS)
    path = journal_path(tmp_path)
    root = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    target = path / entry
    outside = path.parent / "retained"
    if mutation == "replacement":
        target.rename(outside)
        target.touch(mode=0o600)
    elif mutation == "symlink":
        target.rename(outside)
        target.symlink_to(outside)
    elif mutation == "hardlink":
        os.link(target, outside)
    else:
        target.chmod(0o640)

    try:
        with pytest.raises(CustodyUnavailable):
            root.require_current()
    finally:
        root.close()

    assert target.exists()
    assert set(DESCRIPTORS) == baseline


def test_native_custody_refuses_directory_replacement_and_partial_provisioning(
    tmp_path: Path,
) -> None:
    baseline = set(DESCRIPTORS)
    path = journal_path(tmp_path)
    root = CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
    retained = path.parent / "retained"
    path.rename(retained)
    path.mkdir(mode=0o700)

    try:
        with pytest.raises(CustodyUnavailable):
            root.require_current()
        with pytest.raises(FileExistsError):
            CustodyNativeDirectory(retained, NativeCustodyPlatform(), provision=True)
        with pytest.raises(FileNotFoundError):
            CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=False)
    finally:
        root.close()

    assert all((retained / name).exists() for name in FILE_NAMES)
    assert set(DESCRIPTORS) == baseline


def test_native_custody_fork_closes_copies_without_unlocking_parent(
    tmp_path: Path,
) -> None:
    path = journal_path(tmp_path)

    result = subprocess.run(
        [sys.executable, "-c", FORK_OWNERSHIP, str(path)],
        capture_output=True,
        timeout=10,
        check=True,
    )

    assert result.stdout == b"closed\n"
    assert result.stderr == b""


def test_successor_denial_requires_original_lifetime_and_latch_anchor(
    tmp_path: Path,
) -> None:
    path = journal_path(tmp_path)
    platform = NativeCustodyPlatform()
    latch = path / "recovery.lock"
    displaced = path.parent / "displaced"
    with (
        closing(CustodyNativeDirectory(path, platform, provision=True)) as root,
        closing(CustodyNativeDirectory(path, platform, provision=False)) as peer,
        closing(CustodyRecoveryLease(root)) as recovery,
        closing(CustodyLifetimeLease(root)) as lifetime,
        closing(CustodyLifetimeLease(peer)) as other_lifetime,
    ):
        with pytest.raises(CustodyUnavailable):
            recovery.deny_for_successor(other_lifetime)
        state = recovery.deny_for_successor(lifetime)
        latch.rename(displaced)
        latch.touch(mode=0o600)
        latch.write_bytes(CustodyLatch.OPEN.value)
        with pytest.raises(CustodyUnavailable):
            recovery.deny_for_successor(lifetime)

    assert state is CustodyLatch.ARMED
    assert displaced.read_bytes() == CustodyLatch.ARMED.value
    assert latch.read_bytes() == CustodyLatch.OPEN.value
