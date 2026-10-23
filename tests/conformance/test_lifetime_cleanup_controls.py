import asyncio
import os
import signal
from contextlib import ExitStack
from pathlib import Path

import pytest

from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.lifetime import ParentLifetime
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
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.conformance.test_parent_lifetime import limits


async def dispose_test_owner(
    owner: ParentLifetime, clock: ControlledLifetimeClock
) -> None:
    closing = owner.request_close(None)
    clock.advance(40_000_000_000)
    for helper in tuple(owner.mutations.helpers):
        if not helper.closed:
            helper.signal(signal.SIGKILL)
            assert await asyncio.wait_for(
                helper.settle(clock.monotonic_ns() + 5_000_000_000), 5
            )
    await asyncio.wait_for(asyncio.shield(closing), 5)
    assert owner.inventory().cleanup_effect_tasks == 0
    assert owner.inventory().cleanup_driver_tasks == 0
    for record in owner.records.values():
        if record.directory is not None:
            record.directory.close()
    owner.mutations.release()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order",
    [
        "scoped_first",
        "full_first",
        "nested_first",
        "nested_last",
        "after_closed",
        "after_unknown",
    ],
)
async def test_full_close_preserves_scoped_report_owners_and_terminal_joins(
    tmp_path: Path, order: str
) -> None:
    clock = ControlledLifetimeClock()
    owner = ParentLifetime(
        clock=clock,
        limits=limits(),
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
    )
    try:
        tree = await owner.reserve(
            ReservationRequest(
                operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
            )
        )
        await owner.acquire(tree.token, new_operation_id(), TemporaryTreeRequest())
        resource_id = tree.token.resource_id
        directory = owner.mutations.directory
        assert directory is not None
        nested = None
        if order.startswith("nested"):
            child = await owner.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(),
                    kind=ResourceKind.TEMPORARY_TREE,
                    parent_resource_id=resource_id,
                )
            )
            await owner.acquire(
                child.token,
                new_operation_id(),
                TemporaryTreeRequest(parent_resource_id=resource_id),
            )
            nested = child.token.resource_id

        if order in ("scoped_first", "nested_first", "nested_last"):
            nested_closing = (
                owner.request_close(nested)
                if nested is not None and order == "nested_first"
                else None
            )
            scoped = owner.request_close(resource_id)
            if nested is not None and order == "nested_last":
                nested_closing = owner.request_close(nested)
            clock.advance(1)
            full = owner.request_close(None)
            if nested_closing is not None:
                nested_report = await asyncio.wait_for(
                    asyncio.shield(nested_closing), 5
                )
                assert nested_report.state is CleanupOutcome.CLOSED
                assert nested_report.resource_id == nested
                assert owner.request_close(nested) is nested_closing
        elif order == "full_first":
            full = owner.request_close(None)
            clock.advance(1)
            scoped = owner.request_close(resource_id)
        else:
            full = owner.request_close(None)
            if order == "after_unknown":
                clock.advance(40_000_000_000)
            await asyncio.wait_for(asyncio.shield(full), 5)
            scoped = owner.request_close(resource_id)
        scoped_report = await asyncio.wait_for(asyncio.shield(scoped), 5)
        full_report = await asyncio.wait_for(asyncio.shield(full), 5)

        expected = (
            CleanupOutcome.UNKNOWN
            if order == "after_unknown"
            else CleanupOutcome.CLOSED
        )
        assert scoped_report.state is expected
        assert full_report.state is expected
        assert scoped_report.resource_id == resource_id
        assert full_report.resource_id is None
        assert owner.request_close(resource_id) is scoped
        assert owner.request_close(None) is full
        assert await owner.close(resource_id) is scoped_report
        assert await owner.close(None) is full_report
        assert directory.closed is (expected is CleanupOutcome.CLOSED)
        assert owner.inventory().helpers.retained_helpers == 0
        if expected is CleanupOutcome.CLOSED:
            assert scoped_report.unresolved_count == 0
            assert full_report.unresolved_count == 0
            assert scoped_report.cleanup_reference is None
            assert full_report.cleanup_reference is None
    finally:
        await dispose_test_owner(owner, clock)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "after"])
async def test_first_scoped_close_during_root_removal_joins_actual_helper_exit(
    tmp_path: Path, phase: str
) -> None:
    with ExitStack() as resources:
        barrier = DirectoryAnchor.open_path(tmp_path)
        resources.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
        reading, writing = os.pipe()
        resources.callback(os.close, reading)
        resources.callback(os.close, writing)
        clock = ControlledLifetimeClock()
        owner = ParentLifetime(
            clock=clock,
            limits=limits(),
            launcher=ProbeIOHelperLauncher(
                tmp_path, writing, barrier.descriptor, phase=phase, pause_at=8
            ),
        )
        try:
            tree = await owner.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
                )
            )
            await owner.acquire(tree.token, new_operation_id(), TemporaryTreeRequest())
            invocation_path = next(tmp_path.rglob(owner.invocation_id))
            full = owner.request_close(None)
            actual_clock = SystemClock()
            deadline = actual_clock.monotonic_ns() + 5_000_000_000
            while (
                not owner.invocation_cleanup.removing_root
                or not owner.mutations.helpers
            ):
                assert actual_clock.monotonic_ns() < deadline
                if full.done():
                    raise AssertionError(full.result())
                wake: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(wake.set_result, None)
                await wake
            helper = owner.mutations.helpers[-1]
            await entered_fifo(helper.process.pid, reading, deadline, actual_clock)

            scoped = owner.request_close(tree.token.resource_id)
            assert not scoped.done()
            assert not full.done()
            assert not helper.anchor.exited
            assert invocation_path.exists() is (phase == "before")
            writer = os.open(
                "stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=barrier.descriptor
            )
            os.close(writer)
            report = await asyncio.wait_for(asyncio.shield(scoped), 5)
            full_report = await asyncio.wait_for(asyncio.shield(full), 5)

            assert report.state is CleanupOutcome.CLOSED
            assert full_report.state is CleanupOutcome.CLOSED
            assert helper.closed
            assert helper.process.returncode == 0
            assert owner.request_close(tree.token.resource_id) is scoped
            assert owner.inventory().helpers.retained_helpers == 0
        finally:
            await dispose_test_owner(owner, clock)
