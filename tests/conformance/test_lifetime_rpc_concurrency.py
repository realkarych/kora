import asyncio
import gc
import signal
import socket
import sys
import tempfile
import weakref
from contextlib import ExitStack
from pathlib import Path

import pytest

from kora.conformance.lifetime import LifetimeError, ParentLifetime
from kora.conformance.transport import LifetimeEndpoint
from kora.protocols.lifetime import (
    Acquisition,
    CleanupOutcome,
    ClosureReport,
    LifetimeReason,
    ProcessDomain,
    ProcessLaunch,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import ChannelError
from tests.conformance.lifetime_helpers import lifetime_failure_state
from tests.conformance.lifetime_support import (
    ControlledLifetimeClock,
    limits,
    stall_barrier,
)
from tests.support.event_loop import turn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slots", "workload"),
    [
        (1, "tree"),
        (16, "tree"),
        (1, "mixed"),
        (16, "mixed"),
        (3, "execution"),
        (1, "execution-near"),
    ],
)
async def test_real_client_subtree_close_fences_stalled_acquire_before_reply(
    tmp_path: Path,
    slots: int,
    workload: str,
) -> None:
    tree_workload = workload in ("tree", "mixed")
    with ExitStack() as resources:
        barrier = stall_barrier(resources, tmp_path)
        pause_at = 12 if tree_workload else 16
        launcher = barrier.launcher(pause_at=pause_at)
        clock = ControlledLifetimeClock()
        owner = ParentLifetime(
            clock=clock,
            limits=limits().model_copy(update={"max_outstanding_operations": slots}),
            launcher=launcher,
        )
        running: asyncio.Task[ClosureReport] | None = None
        replay_inputs: list[weakref.ReferenceType[ProcessLaunch]] = []
        direct_acquisitions: list[asyncio.Task[Acquisition]] = []
        probe_ready_remaining_ns: int | None = None
        try:
            tree = await owner.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
                )
            )
            await owner.acquire(tree.token, new_operation_id(), TemporaryTreeRequest())
            child = await owner.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(),
                    kind=ResourceKind.PROCESS_DOMAIN,
                    parent_resource_id=tree.token.resource_id,
                )
            )
            with tempfile.TemporaryDirectory(prefix="kora-rpc-", dir="/tmp") as root:
                address = str(Path(root) / "rpc")
                feedback_address = str(Path(root) / "feedback")
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                resources.callback(listener.close)
                listener.bind(address)
                listener.listen(1)
                listener.setblocking(False)
                feedback = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                resources.callback(feedback.close)
                feedback.bind(feedback_address)
                feedback.setblocking(False)
                acquired = await owner.acquire(
                    child.token,
                    new_operation_id(),
                    ProcessLaunch(
                        executable=sys.executable,
                        argv=(
                            sys.executable,
                            "-I",
                            str(Path(__file__).with_name("lifetime_rpc_probe.py")),
                            address,
                            "replay-near"
                            if workload == "execution-near"
                            else "replay-bytes"
                            if not tree_workload
                            else f"concurrent-{1 if workload == 'mixed' else slots}",
                            feedback_address,
                        ),
                        environment=(),
                        working_directory_resource_id=tree.token.resource_id,
                        stdin=None,
                        stdout=None,
                        stderr=None,
                        domain=ProcessDomain.INVOCATION,
                    ),
                )
                await owner.acknowledge(
                    child.token, acquired.operation_id, acquired.revision
                )
                loop = asyncio.get_running_loop()
                endpoint, _ = await asyncio.wait_for(loop.sock_accept(listener), 5)
                parent = LifetimeEndpoint(owner, child.token.resource_id, endpoint)
                running = asyncio.create_task(parent.run())
                await launcher.wait_for_start(pause_at, running)
                actual_clock = SystemClock()
                deadline = actual_clock.monotonic_ns() + 5_000_000_000

                writer = resources.enter_context(
                    await barrier.entered(deadline, actual_clock)
                )
                helper = owner.mutations.helpers[-1]
                probe_ready_remaining_ns = deadline - actual_clock.monotonic_ns()
                if not tree_workload:
                    assert any(
                        isinstance(operation.request, ProcessLaunch)
                        and not operation.task.done()
                        for operation in owner.operations.values()
                    ), tuple(
                        (key, type(operation.request).__name__, operation.task.done())
                        for key, operation in owner.operations.items()
                    )
                payload, peer = await asyncio.wait_for(
                    loop.sock_recvfrom(feedback, 256), 5
                )
                selected = payload.decode("ascii")
                assert selected in parent.resources
                if workload == "mixed":
                    operation_id = next(
                        key[1]
                        for key, operation in owner.operations.items()
                        if operation.resource_id == selected
                        and isinstance(operation.request, TemporaryTreeRequest)
                        and not operation.task.done()
                    )
                    direct_acquisitions = [
                        asyncio.create_task(
                            owner.acquire(
                                owner.records[selected].token,
                                operation_id,
                                TemporaryTreeRequest(),
                            )
                        )
                        for _ in range(slots - 1)
                    ]
                admission_deadline = actual_clock.monotonic_ns() + 5_000_000_000
                while len(owner.ordinary_slots) != slots:
                    assert actual_clock.monotonic_ns() < admission_deadline
                    for direct in direct_acquisitions:
                        if direct.done():
                            raise AssertionError(await direct)
                    await turn()
                assert owner.transport_pending == (1 if workload == "mixed" else slots)
                assert owner.inventory().pending_operations == 1
                if workload == "mixed":
                    with pytest.raises(LifetimeError) as full:
                        await owner.observe(None)
                    assert full.value.reason is LifetimeReason.CAPACITY_EXCEEDED
                    assert len(owner.ordinary_slots) == slots
                    assert (
                        owner.inventory().control_bytes
                        + owner.mutations.inventory().journal_bytes
                        <= owner.limits.max_control_recovery_bytes
                    )
                    if direct_acquisitions:
                        assert owner.direct_control_bytes > 0
                        direct_acquisitions[0].cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await direct_acquisitions[0]
                        assert len(owner.ordinary_slots) == slots - 1
                        assert owner.inventory().pending_operations == 1
                        acquisition_id = owner.records[selected].entry.acquisition_id
                        assert acquisition_id is not None
                        direct_acquisitions.append(
                            asyncio.create_task(
                                owner.acquire(
                                    owner.records[selected].token,
                                    acquisition_id,
                                    TemporaryTreeRequest(),
                                )
                            )
                        )
                        readmission_deadline = (
                            actual_clock.monotonic_ns() + 5_000_000_000
                        )
                        while len(owner.ordinary_slots) != slots:
                            assert actual_clock.monotonic_ns() < readmission_deadline
                            if direct_acquisitions[-1].done():
                                raise AssertionError(await direct_acquisitions[-1])
                            await turn()
                if workload == "execution":
                    assert owner.transport_execution_bytes >= 600000
                    recorded = {
                        id(operation.request) for operation in owner.operations.values()
                    }
                    replay_inputs = [
                        weakref.ref(candidate)
                        for candidate in gc.get_objects()
                        if isinstance(candidate, ProcessLaunch)
                        and candidate.working_directory_resource_id == selected
                        and any(
                            variable.name == "PAYLOAD0"
                            for variable in candidate.environment
                        )
                        and id(candidate) not in recorded
                    ]
                    assert len(replay_inputs) == 2
                    await loop.sock_sendto(feedback, b"cancel", peer)
                    assert (
                        await asyncio.wait_for(loop.sock_recv(feedback, 1), 5) == b"c"
                    )
                    assert owner.transport_execution_bytes >= 600000
                    assert owner.transport_pending == slots
                if workload == "execution-near":
                    assert owner.execution_bytes >= 900000
                    assert owner.transport_execution_bytes == 0
                if not tree_workload:
                    assert (
                        owner.execution_bytes + owner.transport_execution_bytes
                        <= owner.limits.max_execution_snapshot_bytes
                    )
                await loop.sock_sendto(feedback, b"close", peer)
                async with asyncio.timeout(1):
                    while not owner.records[selected].fenced:
                        await asyncio.create_task(owner.cleanup_changed.wait())
                assert not helper.anchor.exited
                assert not owner.records[tree.token.resource_id].fenced
                assert not owner.records[child.token.resource_id].fenced
                writer.close()
                async with asyncio.timeout(10):
                    for direct in direct_acquisitions:
                        if not direct.cancelled():
                            with pytest.raises(LifetimeError):
                                await direct
                    report = await running

                assert report.state is CleanupOutcome.CLOSED, (
                    report.model_dump_json(),
                    owner.mutations.failed,
                    owner.invocation_cleanup.failed,
                    tuple(
                        (
                            key,
                            value.cleanup.failed if value.cleanup is not None else None,
                        )
                        for key, value in owner.records.items()
                    ),
                    tuple(
                        (
                            key,
                            value.process.returncode
                            if value.process is not None
                            else None,
                        )
                        for key, value in owner.records.items()
                    ),
                )
                assert owner.endpoint_count == 0
                assert owner.transport_control_bytes == 0
                assert owner.transport_execution_bytes == 0
                assert not owner.ordinary_slots
                assert owner.transport_pending == 0
                process = owner.records[child.token.resource_id].process
                assert process is not None
                assert process.returncode == 0
                gc.collect()
                assert all(reference() is None for reference in replay_inputs)
        except (ChannelError, TimeoutError) as failure:
            failure.add_note(f"probe_starts={launcher.starts}")
            failure.add_note(f"probe_ready_remaining_ns={probe_ready_remaining_ns}")
            failure.add_note(lifetime_failure_state(owner))
            raise
        finally:
            for direct in direct_acquisitions:
                direct.cancel()
            await asyncio.gather(*direct_acquisitions, return_exceptions=True)
            closing = owner.request_close(None)
            clock.advance(20_000_000_000)
            cleanup_deadline = clock.monotonic_ns() + 5_000_000_000
            for record in owner.records.values():
                process = record.process
                if process is not None and not process.closed:
                    process.signal(signal.SIGKILL, cleanup_deadline)
                    await asyncio.wait_for(process.settle(cleanup_deadline), 5)
                    await asyncio.wait_for(process.drain(cleanup_deadline), 5)
                    process.release(cleanup_deadline)
            for helper in tuple(owner.mutations.helpers):
                if not helper.closed:
                    helper.signal(signal.SIGKILL)
                    assert await asyncio.wait_for(helper.settle(cleanup_deadline), 5)
            await asyncio.wait_for(asyncio.shield(closing), 5)
            if running is not None:
                await asyncio.wait_for(running, 5)
