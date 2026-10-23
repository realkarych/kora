import asyncio
import os
import signal
import socket
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

import pytest

from kora.conformance.lifetime import ParentLifetime
from kora.conformance.transport import LifetimeEndpoint
from kora.protocols.lifetime import (
    CleanupOutcome,
    ClosureReport,
    ProcessDomain,
    ProcessLaunch,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.filesystem import DirectoryAnchor
from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
from tests.conformance.test_lifetime_io_helper import read_probe
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.conformance.test_parent_lifetime import limits


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slots", "workload"),
    [(1, "tree"), (16, "tree"), (3, "execution"), (1, "execution-near")],
)
async def test_real_client_subtree_close_fences_stalled_acquire_before_reply(
    tmp_path: Path,
    slots: int,
    workload: str,
) -> None:
    with ExitStack() as resources:
        barrier = DirectoryAnchor.open_path(tmp_path)
        resources.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
        reading, writing = os.pipe()
        resources.callback(os.close, reading)
        resources.callback(os.close, writing)
        launcher = ProbeIOHelperLauncher(
            tmp_path,
            writing,
            barrier.descriptor,
            pause_at=12 if workload == "tree" else 16,
        )
        clock = ControlledLifetimeClock()
        owner = ParentLifetime(
            clock=clock,
            limits=limits().model_copy(update={"max_outstanding_operations": slots}),
            launcher=launcher,
        )
        running: asyncio.Task[ClosureReport] | None = None
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
                            if workload != "tree"
                            else f"concurrent-{slots}",
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
                actual_clock = SystemClock()
                deadline = actual_clock.monotonic_ns() + 5_000_000_000

                assert await read_probe(reading, deadline, actual_clock) == b"r"
                helper = owner.mutations.helpers[-1]
                while True:
                    os.kill(helper.process.pid, signal.SIGUSR1)
                    if await read_probe(reading, deadline, actual_clock) == b"e":
                        break
                if workload != "tree":
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
                while len(owner.ordinary_slots) != slots:
                    assert actual_clock.monotonic_ns() < deadline
                    admission_ready: asyncio.Future[None] = loop.create_future()
                    loop.call_soon(admission_ready.set_result, None)
                    await admission_ready
                assert owner.transport_pending == slots
                assert owner.inventory().pending_operations == 1
                if workload == "execution":
                    assert owner.transport_execution_bytes >= 600000
                    await loop.sock_sendto(feedback, b"cancel", peer)
                    assert (
                        await asyncio.wait_for(loop.sock_recv(feedback, 1), 5) == b"c"
                    )
                    assert owner.transport_execution_bytes >= 600000
                    assert owner.transport_pending == slots
                if workload == "execution-near":
                    assert owner.execution_bytes >= 900000
                    assert owner.transport_execution_bytes == 0
                if workload != "tree":
                    assert (
                        owner.execution_bytes + owner.transport_execution_bytes
                        <= owner.limits.max_execution_snapshot_bytes
                    )
                await loop.sock_sendto(feedback, b"close", peer)
                fence_deadline = actual_clock.monotonic_ns() + 1_000_000_000
                while not owner.records[selected].fenced:
                    assert actual_clock.monotonic_ns() < fence_deadline
                    ready: asyncio.Future[None] = loop.create_future()
                    loop.call_soon(ready.set_result, None)
                    await ready
                assert not helper.anchor.exited
                assert not owner.records[tree.token.resource_id].fenced
                assert not owner.records[child.token.resource_id].fenced
                writer = os.open(
                    "stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=barrier.descriptor
                )
                os.close(writer)
                report = await asyncio.wait_for(running, 10)

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
        finally:
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
