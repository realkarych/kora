import asyncio
import os
from contextlib import ExitStack
from pathlib import Path

import pytest

from kora.conformance.controllers import duration_ns
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
from tests.conformance.test_lifetime_io_helper import entered_fifo
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
    with ExitStack() as resources:
        barrier = DirectoryAnchor.open_path(tmp_path)
        resources.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
        reading, writing = os.pipe()
        resources.callback(os.close, reading)
        resources.callback(os.close, writing)
        launcher = ProbeIOHelperLauncher(
            tmp_path, writing, barrier.descriptor, pause_at=4
        )
        clock = ControlledLifetimeClock()
        actual = SystemClock()
        owner = ParentLifetime(
            limits=limits().model_copy(update={"acquisition_seconds": 1.0}),
            launcher=launcher,
            clock=clock,
        )
        operation = None
        try:
            async with asyncio.timeout(10):
                tree = await owner.reserve(
                    ReservationRequest(
                        operation_id=new_operation_id(),
                        kind=ResourceKind.TEMPORARY_TREE,
                    )
                )
                operation_id = new_operation_id()
                operation = asyncio.create_task(
                    owner.acquire(tree.token, operation_id, TemporaryTreeRequest())
                )
                deadline = actual.monotonic_ns() + 5_000_000_000
                writer = resources.enter_context(
                    await entered_fifo(barrier.descriptor, reading, deadline, actual)
                )
                helper = owner.mutations.helpers[-1]

                if case == "timeout":
                    clock.advance(duration_ns(owner.limits.acquisition_seconds) + 1)
                    with pytest.raises(LifetimeError):
                        await operation
                    assert owner.mutations.pending == 0
                    assert not helper.anchor.exited
                    closing = owner.request_close(None)
                    clock.advance(duration_ns(owner.limits.cooperative_seconds) + 1)
                    await helper.anchor.wait(deadline, actual)
                    report = await closing
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
                    writer.close()
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
            from tests.conformance.test_lifetime_cleanup_controls import (
                dispose_test_owner,
            )

            try:
                await dispose_test_owner(owner, clock)
            finally:
                if operation is not None:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
async def test_mutation_queue_deadline_does_not_cancel_earlier_publisher(
    tmp_path: Path,
) -> None:
    barrier = DirectoryAnchor.open_path(tmp_path)
    os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
    reading, writing = os.pipe()
    launcher = ProbeIOHelperLauncher(tmp_path, writing, barrier.descriptor, pause_at=3)
    clock = ControlledLifetimeClock()
    actual = SystemClock()
    owner = ParentLifetime(limits=limits(), launcher=launcher, clock=clock)
    tree = await owner.reserve(
        ReservationRequest(
            operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
        )
    )
    operation = asyncio.create_task(
        owner.acquire(tree.token, new_operation_id(), TemporaryTreeRequest())
    )
    deadline = actual.monotonic_ns() + 5_000_000_000
    with await entered_fifo(barrier.descriptor, reading, deadline, actual) as writer:
        helper = owner.mutations.helpers[-1]
        directory = owner.mutations.directory
        assert directory is not None
        queued = CreateTreeMutation(directory=directory.identity, name="e" * 32)
        queued_call = asyncio.create_task(
            owner.mutations.perform(
                queued, directory, clock.monotonic_ns() + 100_000_000
            )
        )

        try:
            while owner.mutations.pending != 2:
                assert actual.monotonic_ns() < deadline
                if queued_call.done():
                    await queued_call
                wake: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(wake.set_result, None)
                await wake
            clock.advance(100_000_000)
            with pytest.raises(HelperError):
                await asyncio.wait_for(queued_call, 1)
            assert not helper.anchor.exited
            assert not operation.done()
            assert owner.mutations.pending == 1
            with pytest.raises(FileNotFoundError):
                os.stat("e" * 32, dir_fd=directory.descriptor, follow_symlinks=False)
        finally:
            writer.close()
            await asyncio.gather(queued_call, return_exceptions=True)
            await asyncio.wait_for(operation, 5)
            report = await asyncio.wait_for(owner.close(None), 5)
            assert report.state is CleanupOutcome.CLOSED
            os.close(reading)
            os.close(writing)
            barrier.close()
