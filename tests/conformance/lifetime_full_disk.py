import asyncio
import errno
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

import pytest

from kora.conformance.io_helper import read_journal
from kora.conformance.lifetime import LifetimeError, ParentLifetime
from kora.protocols.lifetime import (
    CleanupOutcome,
    LifetimeReason,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    ProcessRegistration,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.filesystem import DirectoryAnchor
from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
from tests.conformance.test_lifetime_io_helper import entered_fifo
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock


def bounded_mount() -> Path:
    assert sys.platform == "linux"
    mount = Path(os.environ["KORA_LIFETIME_FULL_DISK_ROOT"])
    assert mount.is_absolute()
    assert mount.resolve(strict=True) == mount
    matches = [
        line.split()
        for line in Path("/proc/self/mountinfo").read_text().splitlines()
        if line.split()[4] == str(mount)
    ]
    assert len(matches) == 1
    row = matches[0]
    assert row[row.index("-") + 1] == "tmpfs"
    capacity = os.statvfs(mount)
    assert 1048576 <= capacity.f_blocks * capacity.f_frsize <= 134217728
    assert mount.stat().st_dev != mount.parent.stat().st_dev
    assert mount.stat().st_uid == os.getuid()
    assert not tuple(mount.iterdir())
    return mount


def fill(descriptor: int, maximum: int) -> int:
    block = b"f" * 1048576
    written = 0
    while written <= maximum:
        try:
            written += os.write(descriptor, block)
        except OSError as failure:
            if failure.errno != errno.ENOSPC:
                raise
            assert written > 0
            return written
    raise AssertionError("bounded tmpfs did not refuse allocation")


@pytest.mark.asyncio
@pytest.mark.parametrize("frontier", ["admission", "registration"])
async def test_full_disk_refuses_without_losing_owned_effects(frontier: str) -> None:
    mount = bounded_mount()
    with tempfile.TemporaryDirectory(dir=mount, prefix="private-") as private:
        home = Path(private)
        with ExitStack() as resources:
            barrier = DirectoryAnchor.open_path(home)
            resources.callback(barrier.close)
            os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
            reading, writing = os.pipe()
            resources.callback(os.close, reading)
            resources.callback(os.close, writing)
            filler = os.open(
                "filler",
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=barrier.descriptor,
            )
            resources.callback(os.close, filler)
            clock = ControlledLifetimeClock()
            launcher = ProbeIOHelperLauncher(
                home,
                writing,
                barrier.descriptor,
                pause_at=1 if frontier == "admission" else 8,
            )
            owner = ParentLifetime(clock=clock, launcher=launcher)
            operation = None
            process = None
            saved = None
            child = None
            try:
                async with asyncio.timeout(10):
                    if frontier == "registration":
                        tree = await owner.reserve(
                            ReservationRequest(
                                operation_id=new_operation_id(),
                                kind=ResourceKind.TEMPORARY_TREE,
                            )
                        )
                        await owner.acquire(
                            tree.token, new_operation_id(), TemporaryTreeRequest()
                        )
                        child = await owner.reserve(
                            ReservationRequest(
                                operation_id=new_operation_id(),
                                kind=ResourceKind.PROCESS_DOMAIN,
                                parent_resource_id=tree.token.resource_id,
                            )
                        )
                        operation = asyncio.create_task(
                            owner.acquire(
                                child.token,
                                new_operation_id(),
                                ProcessLaunch(
                                    executable=sys.executable,
                                    argv=(
                                        sys.executable,
                                        "-I",
                                        "-c",
                                        "raise SystemExit(99)",
                                    ),
                                    environment=(),
                                    working_directory_resource_id=tree.token.resource_id,
                                    stdin=None,
                                    stdout=ProcessOutput.CAPTURE,
                                    stderr=ProcessOutput.CAPTURE,
                                    domain=ProcessDomain.INVOCATION,
                                ),
                            )
                        )
                    actual = SystemClock()
                    deadline = actual.monotonic_ns() + 5_000_000_000
                    while launcher.starts != launcher.pause_at:
                        if operation is not None and operation.done():
                            raise AssertionError(await operation)
                        assert actual.monotonic_ns() < deadline
                        wake: asyncio.Future[None] = (
                            asyncio.get_running_loop().create_future()
                        )
                        asyncio.get_running_loop().call_soon(wake.set_result, None)
                        await wake
                    helper = owner.mutations.helpers[-1]
                    await entered_fifo(helper.process.pid, reading, deadline, actual)
                    if frontier == "registration":
                        assert owner.mutations.directory is not None
                        saved = read_journal(owner.mutations.directory)
                        assert child is not None
                        record = owner.records[child.token.resource_id]
                        process = record.process
                        assert process is not None
                        assert isinstance(
                            record.entry.registration, ProcessRegistration
                        )
                        assert process.returncode is None
                    fill(filler, 134217728)
                    writer = os.open(
                        "stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=barrier.descriptor
                    )
                    os.close(writer)

                    completion = owner.ready() if operation is None else operation
                    with pytest.raises(LifetimeError) as refused:
                        await completion

                    assert refused.value.reason is LifetimeReason.JOURNAL_UNAVAILABLE
                    if saved is not None:
                        assert owner.mutations.directory is not None
                        assert read_journal(owner.mutations.directory) == saved
                    os.ftruncate(filler, 0)
                    closed = await owner.close(None)
                    assert closed.state is CleanupOutcome.UNKNOWN
                    if process is not None:
                        assert process.closed
                        assert process.returncode != 99
                        assert process.readiness() == ()
                    assert owner.inventory().helpers.live_helpers == 0
                    assert owner.inventory().retained_processes == 0
            finally:
                os.ftruncate(filler, 0)
                await dispose_test_owner(owner, clock)
                if operation is not None:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
