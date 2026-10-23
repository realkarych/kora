import asyncio
import os
import signal
from pathlib import Path

import pytest

from kora.conformance.io_helper import CreateTreeMutation, HelperError
from kora.conformance.lifetime import LifetimeError, ParentLifetime
from kora.protocols.lifetime import (
    CleanupOutcome,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.filesystem import DirectoryAnchor
from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
from tests.conformance.test_lifetime_io_helper import read_probe
from tests.conformance.test_parent_lifetime import limits


class ControlledLifetimeClock(SystemClock):
    def __init__(self) -> None:
        self.instant = super().monotonic_ns()
        self.waiters: dict[asyncio.Future[None], int] = {}

    def monotonic_ns(self) -> int:
        return self.instant

    async def sleep_until(self, deadline_ns: int) -> None:
        if self.instant >= deadline_ns:
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiters[waiter] = deadline_ns
        try:
            await waiter
        finally:
            self.waiters.pop(waiter)

    def advance(self, nanoseconds: int) -> None:
        self.instant += nanoseconds
        for waiter, deadline in tuple(self.waiters.items()):
            if self.instant >= deadline and not waiter.done():
                waiter.set_result(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["cancel_during", "late_acquire", "timeout"])
async def test_conformance_fixture_resources_are_released_with_pending_helper(
    tmp_path: Path, case: str
) -> None:
    barrier = DirectoryAnchor.open_path(tmp_path)
    os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
    reading, writing = os.pipe()
    launcher = ProbeIOHelperLauncher(tmp_path, writing, barrier.descriptor, pause_at=4)
    owner = ParentLifetime(
        limits=limits().model_copy(update={"acquisition_seconds": 1.0}),
        launcher=launcher,
        clock=ControlledLifetimeClock() if case != "timeout" else None,
    )
    tree = await owner.reserve(
        ReservationRequest(
            operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
        )
    )
    operation_id = new_operation_id()
    operation = asyncio.create_task(
        owner.acquire(tree.token, operation_id, TemporaryTreeRequest())
    )
    deadline = owner.clock.monotonic_ns() + 5_000_000_000
    assert await read_probe(reading, deadline, owner.clock) == b"r"
    helper = owner.mutations.helpers[-1]
    while True:
        os.kill(helper.process.pid, signal.SIGUSR1)
        response = await read_probe(reading, deadline, owner.clock)
        if response == b"e":
            break
        assert response == b"n"

    try:
        if case == "timeout":
            with pytest.raises(LifetimeError):
                await operation
            assert owner.mutations.pending == 0
            assert not helper.anchor.exited
            report = await owner.close(None)
            assert report.state is CleanupOutcome.UNKNOWN
        else:
            closing = None
            if case == "cancel_during":
                operation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await operation
            else:
                closing = asyncio.create_task(owner.close(None))
                await owner.closure_started.wait()
            writer = os.open(
                "stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=barrier.descriptor
            )
            os.close(writer)
            if case == "cancel_during":
                acquisition = await owner.acquire(
                    tree.token, operation_id, TemporaryTreeRequest()
                )
                assert acquisition.token == tree.token
                report = await owner.close(None)
            else:
                with pytest.raises(LifetimeError):
                    await operation
                assert closing is not None
                report = await asyncio.wait_for(closing, 5)
            assert report.state is CleanupOutcome.CLOSED

        assert helper.anchor.exited
        assert helper.closed
        assert owner.inventory().helpers.live_helpers == 0
    finally:
        await owner.close(None)
        if not helper.closed:
            helper.signal(signal.SIGKILL)
            assert await helper.settle(deadline)
        os.close(reading)
        os.close(writing)
        barrier.close()


@pytest.mark.asyncio
async def test_mutation_queue_deadline_does_not_cancel_earlier_publisher(
    tmp_path: Path,
) -> None:
    barrier = DirectoryAnchor.open_path(tmp_path)
    os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
    reading, writing = os.pipe()
    launcher = ProbeIOHelperLauncher(tmp_path, writing, barrier.descriptor, pause_at=3)
    owner = ParentLifetime(limits=limits(), launcher=launcher)
    tree = await owner.reserve(
        ReservationRequest(
            operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
        )
    )
    operation = asyncio.create_task(
        owner.acquire(tree.token, new_operation_id(), TemporaryTreeRequest())
    )
    deadline = owner.deadline()
    assert await read_probe(reading, deadline, owner.clock) == b"r"
    helper = owner.mutations.helpers[-1]
    while True:
        os.kill(helper.process.pid, signal.SIGUSR1)
        if await read_probe(reading, deadline, owner.clock) == b"e":
            break
    directory = owner.mutations.directory
    assert directory is not None
    queued = CreateTreeMutation(directory=directory.identity, name="e" * 32)

    try:
        with pytest.raises(HelperError):
            await asyncio.wait_for(
                owner.mutations.perform(
                    queued, directory, owner.clock.monotonic_ns() + 100_000_000
                ),
                1,
            )
        assert not helper.anchor.exited
        assert not operation.done()
        assert owner.mutations.pending == 1
        with pytest.raises(FileNotFoundError):
            os.stat("e" * 32, dir_fd=directory.descriptor, follow_symlinks=False)
    finally:
        writer = os.open(
            "stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=barrier.descriptor
        )
        os.close(writer)
        await operation
        report = await owner.close(None)
        assert report.state is CleanupOutcome.CLOSED
        os.close(reading)
        os.close(writing)
        barrier.close()
