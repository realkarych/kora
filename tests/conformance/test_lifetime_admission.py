import asyncio
import os
import signal
from contextlib import ExitStack
from pathlib import Path

import pytest

from kora.conformance.lifetime import LifetimeError, ParentLifetime
from kora.protocols.lifetime import (
    CleanupOutcome,
    LifetimeReason,
    Reservation,
    ReservationRequest,
    ResourceKind,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.filesystem import DirectoryAnchor
from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
from tests.conformance.test_lifetime_io_helper import entered_fifo
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.conformance.test_parent_lifetime import limits


async def reserve_started(
    owner: ParentLifetime, request: ReservationRequest, started: asyncio.Event
) -> Reservation:
    started.set()
    return await owner.reserve(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [1, 16])
async def test_direct_bootstrap_waiters_hold_bounded_slots_before_readiness(
    tmp_path: Path, capacity: int
) -> None:
    with ExitStack() as resources:
        barrier = DirectoryAnchor.open_path(tmp_path)
        resources.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
        reading, writing = os.pipe()
        resources.callback(os.close, reading)
        resources.callback(os.close, writing)
        launcher = ProbeIOHelperLauncher(
            tmp_path, writing, barrier.descriptor, pause_at=1
        )
        clock = ControlledLifetimeClock()
        owner = ParentLifetime(
            launcher=launcher,
            clock=clock,
            limits=limits().model_copy(update={"max_outstanding_operations": capacity}),
        )
        callers: list[asyncio.Task[Reservation]] = []
        try:
            loop = asyncio.get_running_loop()
            deadline = SystemClock().monotonic_ns() + 5_000_000_000
            while not owner.mutations.helpers:
                assert SystemClock().monotonic_ns() < deadline
                if owner.bootstrap_finished is not None:
                    await owner.ready()
                    raise AssertionError("bootstrap ended before helper frontier")
                wake: asyncio.Future[None] = loop.create_future()
                loop.call_soon(wake.set_result, None)
                await wake
            writer = resources.enter_context(
                await entered_fifo(barrier.descriptor, reading, deadline, SystemClock())
            )
            request = ReservationRequest(
                operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
            )
            started = [asyncio.Event() for _ in range(capacity)]
            callers.extend(
                asyncio.create_task(reserve_started(owner, request, event))
                for event in started
            )
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in started)), 5
            )

            assert len(owner.ordinary_slots) == capacity
            waiting = len(clock.waiters)
            with pytest.raises(LifetimeError) as refused:
                await owner.reserve(request)
            assert refused.value.reason is LifetimeReason.CAPACITY_EXCEEDED
            assert len(clock.waiters) == waiting
            callers[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await callers[0]
            assert len(owner.ordinary_slots) == capacity - 1
            replacement = asyncio.create_task(owner.reserve(request))
            callers.append(replacement)
            writer.close()
            reservations = await asyncio.wait_for(asyncio.gather(*callers[1:]), 5)
            report = await asyncio.wait_for(owner.close(None), 5)

            assert all(reservation == reservations[0] for reservation in reservations)
            assert len(owner.records) == 1
            assert len(owner.operations) == 1
            assert not owner.ordinary_slots
            assert not clock.waiters
            assert report.state is CleanupOutcome.CLOSED
        finally:
            for caller in callers:
                caller.cancel()
            for caller in callers:
                try:
                    await caller
                except asyncio.CancelledError:
                    if not caller.cancelled():
                        raise
            closing = owner.request_close(None)
            clock.advance(20_000_000_000)
            for retained in tuple(owner.mutations.helpers):
                if not retained.closed:
                    retained.signal(signal.SIGKILL)
                    await asyncio.wait_for(
                        retained.settle(clock.monotonic_ns() + 5_000_000_000), 5
                    )
            await asyncio.wait_for(asyncio.shield(closing), 5)
