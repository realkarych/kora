import asyncio
import gc
import os
import signal
from contextlib import AsyncExitStack, ExitStack
from pathlib import Path
from types import CoroutineType

import pytest

from kora.conformance.controllers import duration_ns
from kora.conformance.io_helper import CreateTreeMutation, HelperError, IOHelper
from kora.conformance.lifetime import LifetimeError, ParentLifetime
from kora.protocols.lifetime import (
    CleanupOutcome,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.identity import IdentityState, NativeIdentityReader
from kora.runner.processes import ExitAnchor
from tests.conformance.lifetime_helpers import (
    ProbeIOHelperLauncher,
    join_helper_operation,
    unwind_owned,
)
from tests.conformance.lifetime_support import (
    ControlledLifetimeClock,
    dispose_test_owner,
    limits,
    stall_barrier,
)
from tests.fixtures.provider_tooling.src.kora_tooling_fixture.clock import ProbeClock
from tests.support.event_loop import turn

__all__ = ["ControlledLifetimeClock"]


@pytest.mark.asyncio
@pytest.mark.parametrize("original_failure", [False, True], ids=["ordinary", "failure"])
async def test_disposal_joins_cleanup_effect_after_unknown_report(
    tmp_path: Path, original_failure: bool
) -> None:
    with ExitStack() as resources:
        barrier = stall_barrier(resources, tmp_path)
        clock = ProbeClock()
        clock.use_system_time()
        actual = SystemClock()
        owner = ParentLifetime(
            limits=limits().model_copy(update={"cooperative_seconds": 15.0}),
            launcher=barrier.launcher(pause_at=3, phase="after"),
            clock=clock,
        )
        original = ValueError("original_test_failure")
        caught_failure: ValueError | None = None

        async def dispose_after_failure() -> None:
            try:
                if original_failure:
                    raise original
            finally:
                await dispose_test_owner(owner, clock)

        try:
            await asyncio.wait_for(owner.ready(), 5)
            tree = await owner.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
                )
            )
            closing = owner.request_close(None)
            writer = resources.enter_context(
                await barrier.entered(actual.monotonic_ns() + 5_000_000_000, actual)
            )
            effect = owner.cleanup_effect
            assert effect is not None
            async with asyncio.timeout(5):
                while True:
                    awaiting: object = effect.task.get_coro()
                    while isinstance(awaiting, CoroutineType):
                        if awaiting.cr_code is ExitAnchor.wait.__code__:
                            break
                        awaiting = awaiting.cr_await
                    if isinstance(awaiting, CoroutineType):
                        break
                    assert not effect.task.done()
                    await turn()
            clock.advance(40_000_000_000)
            owner.request_close(tree.token.resource_id)
            async with asyncio.timeout(5):
                report = await closing
            assert report.state is CleanupOutcome.UNKNOWN
            assert not effect.task.done()
            helper = owner.mutations.helpers[-1]
            helper.signal(signal.SIGKILL)
            async with asyncio.timeout(5):
                await helper.anchor.readiness
            assert helper.release()
            assert not effect.task.done()

            if original_failure:
                with pytest.raises(
                    ValueError, match="^original_test_failure$"
                ) as failure:
                    await dispose_after_failure()
                caught_failure = failure.value
            else:
                await dispose_after_failure()

            assert caught_failure is (original if original_failure else None)
            assert effect.task.done()
            inventory = owner.inventory()
            assert inventory.cleanup_effect_tasks == 0
            assert inventory.cleanup_driver_tasks == 0
            assert inventory.pending_operations == 0
            assert inventory.helpers.live_helpers == 0
            assert inventory.helpers.directory_descriptors == 0
            assert inventory.helpers.claim_descriptors == 0
            writer.close()
        finally:
            owner.request_close(None)
            clock.advance(40_000_000_000)
            async with asyncio.timeout(5):
                for helper in tuple(owner.mutations.helpers):
                    if not helper.closed:
                        helper.signal(signal.SIGKILL)
                        assert await helper.settle(clock.monotonic_ns() + 5_000_000_000)
                dependencies = owner.recovery_dependencies()
                if dependencies:
                    await asyncio.wait(dependencies)
                assert owner.mutations.dispose_settled()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["cancel_during", "late_acquire", "timeout"])
async def test_conformance_fixture_resources_are_released_with_pending_helper(
    tmp_path: Path, case: str
) -> None:
    with ExitStack() as resources:
        barrier = stall_barrier(resources, tmp_path)
        launcher = barrier.launcher(pause_at=4)
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
                    await barrier.entered(deadline, actual)
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
            try:
                await dispose_test_owner(owner, clock)
            finally:
                if operation is not None:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)


async def exercise_mutation_queue_deadline(
    tmp_path: Path,
    fail_barrier: bool,
    owners: list[ParentLifetime],
    helpers: list[IOHelper],
    waiting: asyncio.Event | None = None,
) -> None:
    clock = ControlledLifetimeClock()
    actual = SystemClock()
    owned = AsyncExitStack()
    try:
        resources = owned.enter_context(ExitStack())
        barrier = stall_barrier(resources, tmp_path)
        launcher = barrier.launcher(pause_at=3)
        owner = ParentLifetime(limits=limits(), launcher=launcher, clock=clock)
        owners.append(owner)
        owned.push_async_callback(dispose_test_owner, owner, clock)
        tree = await owner.reserve(
            ReservationRequest(
                operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
            )
        )
        operation = asyncio.create_task(
            owner.acquire(tree.token, new_operation_id(), TemporaryTreeRequest())
        )
        owned.push_async_callback(join_helper_operation, operation)
        deadline = actual.monotonic_ns() + 5_000_000_000
        await launcher.wait_for_start(3, operation)
        helpers.extend(owner.mutations.helpers)
        entered = actual.monotonic_ns() if fail_barrier else deadline
        if waiting is not None:
            waiting.set()
        writer = await barrier.entered(entered, actual)
        owned.callback(writer.close)
        helper = owner.mutations.helpers[-1]
        directory = owner.mutations.directory
        assert directory is not None
        queued = CreateTreeMutation(directory=directory.identity, name="e" * 32)
        queued_call = asyncio.create_task(
            owner.mutations.perform(
                queued, directory, clock.monotonic_ns() + 100_000_000
            )
        )
        observed_errors: list[BaseException] = []
        owned.push_async_callback(join_helper_operation, queued_call, observed_errors)

        while owner.mutations.pending != 2:
            assert actual.monotonic_ns() < deadline
            if queued_call.done():
                await queued_call
            await turn()
        clock.advance(100_000_000)
        with pytest.raises(HelperError) as failure:
            await asyncio.wait_for(queued_call, 1)
        observed_errors.append(failure.value)
        assert not helper.anchor.exited
        assert not operation.done()
        assert owner.mutations.pending == 1
        with pytest.raises(FileNotFoundError):
            os.stat("e" * 32, dir_fd=directory.descriptor, follow_symlinks=False)
        writer.close()
        await asyncio.wait_for(operation, 5)
        report = await asyncio.wait_for(owner.close(None), 5)

        assert report.state is CleanupOutcome.CLOSED
    finally:
        await unwind_owned(owned)


@pytest.mark.asyncio
async def test_mutation_queue_deadline_does_not_cancel_earlier_publisher(
    tmp_path: Path,
) -> None:
    await exercise_mutation_queue_deadline(tmp_path, False, [], [])


@pytest.mark.asyncio
async def test_pending_publisher_barrier_failure_releases_acquired_resources(
    tmp_path: Path,
) -> None:
    gc.collect()
    baseline = set(os.listdir("/dev/fd"))
    owners: list[ParentLifetime] = []
    helpers: list[IOHelper] = []
    helper: IOHelper | None = None
    try:
        with pytest.raises(ChannelError) as failure:
            await exercise_mutation_queue_deadline(tmp_path, True, owners, helpers)

        assert failure.value.reason is ChannelFailure.DEADLINE
        assert len(owners) == 1
        owner = owners[0]
        assert helpers
        reader = NativeIdentityReader()
        for helper in helpers:
            assert helper.closed
            assert helper.anchor.exited
            assert helper.process.returncode is not None
            assert reader.read(helper.process.pid).state is IdentityState.ABSENT
            group = reader.group(helper.process.pid)
            assert group.state is IdentityState.PRESENT
            assert group.members == ()
        inventory = owner.inventory()
        assert inventory.retained_processes == 0
        assert inventory.pending_operations == 0
        assert inventory.cleanup_effect_tasks == 0
        assert inventory.cleanup_driver_tasks == 0
        assert inventory.retained_trees == 0
        assert inventory.helpers.claim_descriptors == 0
        assert inventory.helpers.directory_descriptors == 0
        del failure
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline
    finally:
        for owner in owners:
            assert isinstance(owner.clock, ControlledLifetimeClock)
            await dispose_test_owner(owner, owner.clock)
    del owners, owner, helpers, helper
    gc.collect()

    assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
async def test_pending_publisher_barrier_cancellation_releases_acquired_resources(
    tmp_path: Path,
) -> None:
    gc.collect()
    baseline = set(os.listdir("/dev/fd"))
    owners: list[ParentLifetime] = []
    helpers: list[IOHelper] = []
    helper: IOHelper | None = None
    waiting = asyncio.Event()
    pending = asyncio.create_task(
        exercise_mutation_queue_deadline(tmp_path, False, owners, helpers, waiting)
    )
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        assert not pending.done()

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        assert len(owners) == 1
        assert helpers
        reader = NativeIdentityReader()
        for helper in helpers:
            assert helper.closed
            assert helper.anchor.exited
            assert helper.process.returncode is not None
            assert reader.read(helper.process.pid).state is IdentityState.ABSENT
            group = reader.group(helper.process.pid)
            assert group.state is IdentityState.PRESENT
            assert group.members == ()
        owner = owners[0]
        inventory = owner.inventory()
        assert inventory.retained_processes == 0
        assert inventory.pending_operations == 0
        assert inventory.cleanup_effect_tasks == 0
        assert inventory.cleanup_driver_tasks == 0
        assert inventory.retained_trees == 0
        assert inventory.helpers.claim_descriptors == 0
        assert inventory.helpers.directory_descriptors == 0
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline
    finally:
        try:
            await join_helper_operation(pending)
        finally:
            for owner in owners:
                assert isinstance(owner.clock, ControlledLifetimeClock)
                await dispose_test_owner(owner, owner.clock)
    del owners, owner, helpers, helper, pending
    gc.collect()

    assert set(os.listdir("/dev/fd")) == baseline
