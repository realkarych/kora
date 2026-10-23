import asyncio
import os
import signal
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from kora.conformance.lifetime import ParentLifetime
from kora.conformance.transport import LifetimeEndpoint
from kora.protocols.lifetime import (
    CleanupOutcome,
    ProcessDomain,
    ProcessLaunch,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.runner.filesystem import DirectoryAnchor
from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
from tests.conformance.test_lifetime_io_helper import read_probe
from tests.conformance.test_parent_lifetime import limits


@pytest.mark.asyncio
@pytest.mark.parametrize("slots", [1, 16])
async def test_real_client_subtree_close_fences_stalled_acquire_before_reply(
    tmp_path: Path,
    slots: int,
) -> None:
    barrier = DirectoryAnchor.open_path(tmp_path)
    os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
    reading, writing = os.pipe()
    launcher = ProbeIOHelperLauncher(tmp_path, writing, barrier.descriptor, pause_at=12)
    owner = ParentLifetime(
        limits=limits().model_copy(update={"max_outstanding_operations": slots}),
        launcher=launcher,
    )
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
        listener.bind(address)
        listener.listen(1)
        listener.setblocking(False)
        feedback = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
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
                    f"concurrent-{slots}",
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
        await owner.acknowledge(child.token, acquired.operation_id, acquired.revision)
        loop = asyncio.get_running_loop()
        endpoint, _ = await asyncio.wait_for(loop.sock_accept(listener), 5)
        parent = LifetimeEndpoint(owner, child.token.resource_id, endpoint)
        running = asyncio.create_task(parent.run())
        released = False
        deadline = owner.deadline()

        try:
            assert await read_probe(reading, deadline, owner.clock) == b"r"
            helper = owner.mutations.helpers[-1]
            while True:
                os.kill(helper.process.pid, signal.SIGUSR1)
                if await read_probe(reading, deadline, owner.clock) == b"e":
                    break
            payload, peer = await asyncio.wait_for(loop.sock_recvfrom(feedback, 256), 5)
            selected = payload.decode("ascii")
            assert selected in parent.resources
            while len(owner.ordinary_slots) != slots:
                assert owner.clock.monotonic_ns() < deadline
                admission_ready: asyncio.Future[None] = loop.create_future()
                loop.call_soon(admission_ready.set_result, None)
                await admission_ready
            assert owner.transport_pending == slots
            assert owner.inventory().pending_operations == 1
            await loop.sock_sendto(feedback, b"close", peer)
            fence_deadline = owner.clock.monotonic_ns() + 1_000_000_000
            while not owner.records[selected].fenced:
                assert owner.clock.monotonic_ns() < fence_deadline
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
            released = True
            report = await asyncio.wait_for(running, 10)

            assert report.state is CleanupOutcome.CLOSED, (
                report.model_dump_json(),
                owner.mutations.failed,
                owner.invocation_cleanup.failed,
                tuple(
                    (key, value.cleanup.failed if value.cleanup is not None else None)
                    for key, value in owner.records.items()
                ),
                tuple(
                    (
                        key,
                        value.process.returncode if value.process is not None else None,
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
            if not released:
                writer = os.open(
                    "stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=barrier.descriptor
                )
                os.close(writer)
            await owner.close(None)
            await running
            cleanup_deadline = owner.clock.monotonic_ns() + 5_000_000_000
            for record in owner.records.values():
                if record.process is not None and not record.process.closed:
                    record.process.signal(signal.SIGKILL, cleanup_deadline)
                    await record.process.settle(cleanup_deadline)
                    await record.process.drain(cleanup_deadline)
                    record.process.release(cleanup_deadline)
            for helper in owner.mutations.helpers:
                if not helper.closed:
                    helper.signal(signal.SIGKILL)
                    assert await helper.settle(cleanup_deadline)
            feedback.close()
            listener.close()
            os.close(reading)
            os.close(writing)
            barrier.close()
