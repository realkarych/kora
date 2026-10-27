import asyncio
import errno
import os
import signal
import sys
from collections.abc import Sequence
from contextlib import ExitStack, nullcontext
from pathlib import Path

import pytest

from kora.conformance.controllers import duration_ns
from kora.conformance.io_helper import InstalledIOHelperLauncher, IOHelper
from kora.conformance.lifetime import ParentLifetime
from kora.protocols.lifetime import (
    CleanupOutcome,
    ClosureReport,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.processes import GatedProcess, RecoveredProcess
from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
from tests.conformance.test_lifetime_io_helper import entered_fifo
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.conformance.test_parent_lifetime import limits


async def close_recovered_test_owner(
    owner: ParentLifetime,
    clock: ControlledLifetimeClock,
    observers: Sequence[RecoveredProcess],
) -> ClosureReport:
    actual = SystemClock()
    closing = owner.request_close(None)
    clock.advance(duration_ns(owner.limits.cooperative_seconds) + 1)
    deadline = actual.monotonic_ns() + 5_000_000_000
    while not all(observer.observe(deadline).settled for observer in observers):
        assert actual.monotonic_ns() < deadline
        wake: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(wake.set_result, None)
        await wake
    if observers and owner.cleanup_effect is None:
        clock.advance(duration_ns(owner.limits.term_seconds) + 1)
    return await asyncio.wait_for(
        asyncio.shield(closing),
        max(0, (deadline - actual.monotonic_ns()) / 1_000_000_000),
    )


async def dispose_test_owner(
    owner: ParentLifetime, clock: ControlledLifetimeClock
) -> None:
    closing = owner.request_close(None)
    clock.advance(40_000_000_000)
    deadline = clock.monotonic_ns() + 5_000_000_000
    async with asyncio.timeout(5):
        for helper in tuple(owner.mutations.helpers):
            if not helper.closed:
                helper.signal(signal.SIGKILL)
                assert await helper.settle(deadline)
        await asyncio.shield(closing)
        assert owner.inventory().cleanup_effect_tasks == 0
        assert owner.inventory().cleanup_driver_tasks == 0
        for record in owner.records.values():
            process = record.process or record.recovered
            if process is not None and not process.closed:
                process.signal(signal.SIGKILL, deadline)
                await process.settle(deadline)
                if isinstance(process, GatedProcess):
                    await process.drain(deadline)
                process.release(deadline)
            if record.directory is not None:
                record.directory.close()
        assert owner.inventory().retained_processes == 0
        assert owner.mutations.dispose_settled()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_before_disposal", [False, True])
async def test_failure_disposal_closes_unadopted_bootstrap_descriptors(
    tmp_path: Path, failure_before_disposal: bool
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
        owner = ParentLifetime(
            clock=clock,
            limits=limits(),
            launcher=ProbeIOHelperLauncher(
                tmp_path, writing, barrier.descriptor, phase="after"
            ),
        )
        deadline = actual.monotonic_ns() + 5_000_000_000
        helper: IOHelper | None = None
        descriptors: tuple[int, ...] = ()
        expected = (
            pytest.raises(RuntimeError, match="original bootstrap fixture failure")
            if failure_before_disposal
            else nullcontext()
        )
        with expected:
            try:
                resources.enter_context(
                    await entered_fifo(barrier.descriptor, reading, deadline, actual)
                )
                helper = owner.mutations.helpers[0]
                while len(helper.descriptors) != 3:
                    assert actual.monotonic_ns() < deadline
                    wake: asyncio.Future[None] = (
                        asyncio.get_running_loop().create_future()
                    )
                    asyncio.get_running_loop().call_soon(wake.set_result, None)
                    await wake
                descriptors = helper.descriptors
                assert not helper.closed
                assert owner.mutations.pending == 1
                for descriptor in descriptors:
                    os.fstat(descriptor)

                if failure_before_disposal:
                    raise RuntimeError("original bootstrap fixture failure")
                await dispose_test_owner(owner, clock)

                assert helper.closed
                assert helper.descriptors == ()
            finally:
                await dispose_test_owner(owner, clock)

        assert helper is not None
        assert len(descriptors) == 3
        assert helper.closed
        assert helper.anchor.exited
        assert helper.process.returncode is not None
        assert helper.endpoint.fileno() == -1
        assert helper.descriptors == ()
        with pytest.raises(ChildProcessError):
            os.waitpid(helper.process.pid, os.WNOHANG)
        for descriptor in descriptors:
            with pytest.raises(OSError, match=os.strerror(errno.EBADF)) as raised:
                os.fstat(descriptor)
            assert raised.value.errno == errno.EBADF
        inventory = owner.inventory()
        assert inventory.cleanup_driver_tasks == 0
        assert inventory.cleanup_effect_tasks == 0
        assert inventory.helpers.live_helpers == 0
        assert inventory.helpers.retained_helpers == 0
        assert inventory.helpers.pending_mutations == 0
        assert inventory.helpers.retained_origins == 0
        assert inventory.helpers.directory_descriptors == 0
        assert inventory.helpers.claim_descriptors == 0
        assert owner.recovery_dependencies() == ()
        assert owner.request_close(None).result().state is CleanupOutcome.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("activated", [False, True])
async def test_failure_disposal_releases_prepared_and_active_process_owners(
    tmp_path: Path, activated: bool
) -> None:
    clock = ControlledLifetimeClock()
    actual = SystemClock()
    owner = ParentLifetime(
        clock=clock,
        limits=limits(),
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
    )
    process: GatedProcess | None = None
    pid: int | None = None

    async def fail_after_preparation() -> None:
        try:
            raise RuntimeError("original owned test failure")
        finally:
            await dispose_test_owner(owner, clock)

    try:
        tree = await owner.reserve(
            ReservationRequest(
                operation_id=new_operation_id(),
                kind=ResourceKind.TEMPORARY_TREE,
            )
        )
        await owner.acquire(tree.token, new_operation_id(), TemporaryTreeRequest())
        reservation = await owner.reserve(
            ReservationRequest(
                operation_id=new_operation_id(),
                kind=ResourceKind.PROCESS_DOMAIN,
                parent_resource_id=tree.token.resource_id,
            )
        )
        acquired = await owner.acquire(
            reservation.token,
            new_operation_id(),
            ProcessLaunch(
                executable=sys.executable,
                argv=(
                    sys.executable,
                    "-I",
                    "-c",
                    "import os, signal; os.write(1, b'x'); signal.pause()",
                ),
                environment=(),
                working_directory_resource_id=tree.token.resource_id,
                stdin=None,
                stdout=ProcessOutput.CAPTURE,
                stderr=ProcessOutput.CAPTURE,
                domain=ProcessDomain.INVOCATION,
            ),
        )
        process = owner.records[reservation.token.resource_id].process
        assert process is not None
        observed = process.observe()
        assert len(observed.live_members) == 1
        pid = observed.live_members[0].pid
        if activated:
            await owner.acknowledge(
                reservation.token, acquired.operation_id, acquired.revision
            )
            deadline = actual.monotonic_ns() + 5_000_000_000
            while process.capture_bytes == 0:
                assert actual.monotonic_ns() < deadline
                wake: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(wake.set_result, None)
                await wake

        with pytest.raises(RuntimeError, match="original owned test failure"):
            await fail_after_preparation()

        assert process is not None
        assert process.closed
        assert owner.request_close(None).result().state is CleanupOutcome.UNKNOWN
        assert process.returncode is not None
        assert process.observe().capture_eof
        assert process.readiness() == ()
        assert owner.inventory().retained_processes == 0
        assert owner.inventory().helpers.retained_helpers == 0
        assert owner.inventory().helpers.claim_descriptors == 0
        assert pid is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
    finally:
        if process is not None and not process.closed:
            deadline = clock.monotonic_ns() + 5_000_000_000
            process.signal(signal.SIGKILL, deadline)
            await asyncio.wait_for(process.settle(deadline), 5)
            await asyncio.wait_for(process.drain(deadline), 5)
            process.release(deadline)
        await dispose_test_owner(owner, clock)


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
            writer = resources.enter_context(
                await entered_fifo(barrier.descriptor, reading, deadline, actual_clock)
            )

            scoped = owner.request_close(tree.token.resource_id)
            assert not scoped.done()
            assert not full.done()
            assert not helper.anchor.exited
            assert invocation_path.exists() is (phase == "before")
            writer.close()
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
