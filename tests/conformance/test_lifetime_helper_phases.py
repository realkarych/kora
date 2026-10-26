import asyncio
import fcntl
import os
import signal
from contextlib import ExitStack
from pathlib import Path

import pytest

from kora.conformance.lifetime import CleanupEffect, LifetimeError, ParentLifetime
from kora.protocols.lifetime import (
    Acquisition,
    CleanupOutcome,
    ReservationRequest,
    ResourceId,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.filesystem import DirectoryAnchor
from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
from tests.conformance.test_lifetime_io_helper import (
    entered_fifo,
    read_probe,
)
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.conformance.test_parent_lifetime import limits


class QueueDeadlineLifetime(ParentLifetime):
    def start_publication(self) -> asyncio.Task[None]:
        return asyncio.create_task(self._persist(self.deadline()))

    def queue_scoped_publication(self, resource_id: ResourceId) -> CleanupEffect:
        control = self.records[resource_id].cleanup
        assert control is not None
        self._start_cleanup_effect(control, resource_id, self._publish_cleanup(control))
        effect = self.cleanup_effect
        assert effect is not None
        return effect


@pytest.mark.asyncio
@pytest.mark.parametrize("slots", [1, 16])
@pytest.mark.parametrize(
    "effect", ["acquire", "remove", "late_remove", "publish", "unrelated", "elapsed"]
)
async def test_selected_helper_keeps_original_scope_and_term_kill_phases(
    tmp_path: Path, effect: str, slots: int
) -> None:
    with ExitStack() as resources:
        barrier = DirectoryAnchor.open_path(tmp_path)
        resources.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
        reading, writing = os.pipe()
        resources.callback(os.close, reading)
        resources.callback(os.close, writing)
        clock = ControlledLifetimeClock()
        launcher = ProbeIOHelperLauncher(
            tmp_path,
            writing,
            barrier.descriptor,
            pause_at=7 if "remove" in effect else 8 if effect == "publish" else 5,
            resist_term=True,
        )
        owner = ParentLifetime(
            clock=clock,
            limits=limits().model_copy(update={"max_outstanding_operations": slots}),
            launcher=launcher,
        )
        acquiring: asyncio.Task[Acquisition] | None = None
        try:
            selected = await owner.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
                )
            )
            sibling = await owner.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
                )
            )
            acquiring = asyncio.create_task(
                owner.acquire(
                    selected.token, new_operation_id(), TemporaryTreeRequest()
                )
            )
            if effect in ("remove", "late_remove", "publish"):
                await acquiring
                closing = owner.request_close(selected.token.resource_id)
                if effect == "late_remove":
                    clock.advance(200_000_000)
            else:
                closing = None
            actual_clock = SystemClock()
            deadline = actual_clock.monotonic_ns() + 5_000_000_000
            while launcher.starts < (launcher.pause_at or 0):
                assert actual_clock.monotonic_ns() < deadline
                if acquiring.done() and effect not in (
                    "remove",
                    "late_remove",
                    "publish",
                ):
                    raise AssertionError(acquiring.result())
                wake: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(wake.set_result, None)
                await wake
            helper = owner.mutations.helpers[-1]
            writer = resources.enter_context(
                await entered_fifo(barrier.descriptor, reading, deadline, actual_clock)
            )
            assert owner.inventory().helpers.retained_origins == 1
            assert len(owner.mutations.helpers) <= slots + 1
            origin = owner.mutations.origins[helper]
            if effect in ("remove", "late_remove", "publish"):
                assert origin.task is None
                assert origin.cleanup is not None
            else:
                assert origin.task is not None
                assert origin.cleanup is None
            if closing is None:
                closing = owner.request_close(
                    sibling.token.resource_id
                    if effect == "unrelated"
                    else selected.token.resource_id
                )
            claim = owner.mutations.claim
            assert claim is not None
            directory = owner.mutations.directory
            assert directory is not None
            independent = os.open("claim", os.O_RDONLY, dir_fd=directory.descriptor)
            resources.callback(os.close, independent)

            if effect in ("unrelated", "elapsed"):
                clock.advance(3_200_000_001)
                report = await asyncio.wait_for(asyncio.shield(closing), 5)
                assert report.state is CleanupOutcome.UNKNOWN
                assert not helper.anchor.exited
                assert owner.mutations.origins[helper].signalled == 0
                assert owner.records[selected.token.resource_id].fenced is (
                    effect == "elapsed"
                )
                if effect == "unrelated":
                    writer.close()
                    await asyncio.wait_for(asyncio.shield(acquiring), 5)
            else:
                clock.advance(100_000_000)
                assert (
                    await read_probe(
                        reading,
                        actual_clock.monotonic_ns() + 5_000_000_000,
                        actual_clock,
                    )
                    == b"t"
                )
                assert not helper.anchor.exited
                clock.advance(100_000_000)
                await asyncio.wait_for(
                    helper.anchor.wait(clock.monotonic_ns() + 5_000_000_000, clock), 5
                )
                report = await asyncio.wait_for(asyncio.shield(closing), 5)
                assert report.state is CleanupOutcome.UNKNOWN
                assert helper.closed
                assert helper.process.returncode == -signal.SIGKILL
                assert not owner.records[sibling.token.resource_id].fenced
                if effect == "acquire":
                    with pytest.raises(LifetimeError):
                        await acquiring
            with pytest.raises(BlockingIOError):
                fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert owner.mutations.claim == claim
        finally:
            if acquiring is not None:
                acquiring.cancel()
                try:
                    await acquiring
                except asyncio.CancelledError:
                    assert acquiring.cancelled()
                except LifetimeError:
                    assert acquiring.done()
            await dispose_test_owner(owner, clock)
            assert owner.inventory().helpers.retained_origins == 0


@pytest.mark.asyncio
async def test_queued_cleanup_keeps_earlier_full_deadline_before_launch(
    tmp_path: Path,
) -> None:
    with ExitStack() as resources:
        barrier = DirectoryAnchor.open_path(tmp_path)
        resources.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
        reading, writing = os.pipe()
        resources.callback(os.close, reading)
        resources.callback(os.close, writing)
        clock = ControlledLifetimeClock()
        actual = SystemClock()
        launcher = ProbeIOHelperLauncher(
            tmp_path, writing, barrier.descriptor, pause_at=3
        )
        owner = QueueDeadlineLifetime(clock=clock, limits=limits(), launcher=launcher)
        publisher: asyncio.Task[None] | None = None
        try:
            tree = await owner.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
                )
            )
            publisher = owner.start_publication()
            deadline = actual.monotonic_ns() + 5_000_000_000
            while launcher.starts < 3:
                assert actual.monotonic_ns() < deadline
                if publisher.done():
                    await publisher
                    raise AssertionError("publisher did not reach held helper")
                wake: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(wake.set_result, None)
                await wake
            helper = owner.mutations.helpers[-1]
            writer = resources.enter_context(
                await entered_fifo(barrier.descriptor, reading, deadline, actual)
            )
            full = owner.request_close(None)
            clock.advance(10_000_000)
            scoped = owner.request_close(tree.token.resource_id)
            control = owner.records[tree.token.resource_id].cleanup
            assert control is not None
            effect = owner.queue_scoped_publication(tree.token.resource_id)
            while owner.mutations.pending != 2:
                assert actual.monotonic_ns() < deadline
                if effect.task.done():
                    await effect.task
                    raise AssertionError("cleanup did not reach publisher queue")
                wake = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(wake.set_result, None)
                await wake
            original_final = owner.invocation_cleanup.final
            assert effect.permit.final == original_final < control.final

            clock.advance(original_final - clock.monotonic_ns() + 1)
            full_report = await asyncio.wait_for(asyncio.shield(full), 5)
            assert full_report.state is CleanupOutcome.UNKNOWN
            assert not helper.anchor.exited
            assert not effect.task.done()
            writer.close()
            await asyncio.wait_for(asyncio.shield(publisher), 5)
            with pytest.raises(LifetimeError):
                await asyncio.wait_for(asyncio.shield(effect.task), 5)
            scoped_report = await asyncio.wait_for(asyncio.shield(scoped), 5)

            assert launcher.starts == 3
            assert helper.process.returncode == 0
            assert helper.closed
            assert effect.permit.final == original_final
            assert scoped_report.state is CleanupOutcome.UNKNOWN
            assert owner.request_close(None).result() is full_report
            assert owner.mutations.snapshot is not None
            assert tuple(owner.records) == tuple(
                entry.resource_id for entry in owner.mutations.snapshot.entries
            )
        finally:
            await dispose_test_owner(owner, clock)
            if publisher is not None:
                await asyncio.gather(publisher, return_exceptions=True)


@pytest.mark.asyncio
async def test_cleanup_refuses_fresh_helper_with_three_nanoseconds_remaining(
    tmp_path: Path,
) -> None:
    with ExitStack() as resources:
        reading, writing = os.pipe()
        resources.callback(os.close, reading)
        resources.callback(os.close, writing)
        launcher = ProbeIOHelperLauncher(tmp_path, writing, pause_at=99)
        clock = ControlledLifetimeClock()
        owner = ParentLifetime(clock=clock, limits=limits(), launcher=launcher)
        try:
            await owner.ready()
            journal = next(tmp_path.rglob(owner.invocation_id)) / "journal"
            before = journal.read_bytes()

            closing = owner.request_close(None)
            final = owner.invocation_cleanup.final
            clock.advance(final - clock.monotonic_ns() - 3)
            report = await asyncio.wait_for(asyncio.shield(closing), 5)

            assert report.state is CleanupOutcome.UNKNOWN
            assert launcher.starts == 1
            assert owner.clock.monotonic_ns() == final - 3
            assert journal.read_bytes() == before
            assert owner.inventory().helpers.retained_origins == 0
            assert owner.inventory().helpers.retained_helpers == 0
        finally:
            await dispose_test_owner(owner, clock)
