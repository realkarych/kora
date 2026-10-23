import asyncio
import signal
import socket
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

import pytest

from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.lifetime import ParentLifetime
from kora.conformance.transport import ENDPOINT_CONTROL_BYTES, LifetimeEndpoint
from kora.protocols.lifetime import (
    CleanupOutcome,
    ClosureReport,
    LifetimeLimits,
    ProcessDomain,
    ProcessLaunch,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["eof", "terminal", "cancel_frame", "cancel_execution"]
)
async def test_conformance_fixture_resources_are_released_through_real_client_process(
    tmp_path: Path, mode: str
) -> None:
    with ExitStack() as resources:
        owner = ParentLifetime(
            limits=LifetimeLimits(
                cooperative_seconds=0.5,
                term_seconds=0.1,
                kill_observation_seconds=2.0,
                drain_seconds=1.0,
            ),
            launcher=InstalledIOHelperLauncher(
                home=tmp_path, state_home=tmp_path / "state"
            ),
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
            with tempfile.TemporaryDirectory(
                prefix="kora-rpc-", dir="/tmp"
            ) as transport_root:
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                resources.callback(listener.close)
                address = str(Path(transport_root) / "rpc")
                feedback_address = str(Path(transport_root) / "feedback")
                feedback = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                resources.callback(feedback.close)
                feedback.bind(feedback_address)
                feedback.setblocking(False)
                listener.bind(address)
                listener.listen(1)
                listener.setblocking(False)
                launch = ProcessLaunch(
                    executable=sys.executable,
                    argv=(
                        sys.executable,
                        "-I",
                        str(Path(__file__).with_name("lifetime_rpc_probe.py")),
                        address,
                        mode,
                        feedback_address,
                    ),
                    environment=(),
                    working_directory_resource_id=tree.token.resource_id,
                    stdin=None,
                    stdout=None,
                    stderr=None,
                    domain=ProcessDomain.INVOCATION,
                )
                acquisition = await owner.acquire(
                    child.token, new_operation_id(), launch
                )
                await owner.acknowledge(
                    child.token, acquisition.operation_id, acquisition.revision
                )
                endpoint, _ = await asyncio.wait_for(
                    asyncio.get_running_loop().sock_accept(listener), 5
                )
                parent = LifetimeEndpoint(owner, child.token.resource_id, endpoint)

                if mode.startswith("cancel"):
                    running = asyncio.create_task(parent.run())
                    loop = asyncio.get_running_loop()
                    for phase in (
                        b"r",
                        b"d",
                        b"t",
                        b"a",
                        b"s",
                        b"q",
                        b"u",
                        b"v",
                        b"o",
                        b"c",
                        b"p",
                    ):
                        assert (
                            await asyncio.wait_for(loop.sock_recv(feedback, 1), 5)
                            == phase
                        )
                    deadline = owner.deadline()
                    while not (
                        owner.transport_execution_bytes
                        if mode == "cancel_execution"
                        else owner.transport_control_bytes - ENDPOINT_CONTROL_BYTES
                    ):
                        assert owner.clock.monotonic_ns() < deadline
                        ready: asyncio.Future[None] = loop.create_future()
                        loop.call_soon(ready.set_result, None)
                        await ready
                    assert not parent.pending
                    assert any(
                        slot.wire and slot.task is None for slot in owner.ordinary_slots
                    )
                    running.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await running
                    report = await owner.close(None)
                else:
                    running = asyncio.create_task(parent.run())
                    report = await running

                assert report.state is CleanupOutcome.CLOSED
                assert owner.endpoint_count == 0
                assert parent.control_bytes == 0
                assert parent.execution_bytes == 0
                assert owner.transport_control_bytes == 0
                assert owner.transport_execution_bytes == 0
                assert owner.inventory().retained_processes == 0
                process = owner.records[child.token.resource_id].process
                assert process is not None
                assert process.returncode == 0
        finally:
            if running is not None:
                running.cancel()
                try:
                    await running
                except asyncio.CancelledError:
                    if not running.cancelled():
                        raise
            await owner.close(None)
            deadline = owner.clock.monotonic_ns() + 5_000_000_000
            for record in owner.records.values():
                process = record.process
                if process is not None and not process.closed:
                    process.signal(signal.SIGKILL, deadline)
                    await process.settle(deadline)
                    await process.drain(deadline)
                    process.release(deadline)
                if record.directory is not None:
                    record.directory.close()
            for helper in owner.mutations.helpers:
                if not helper.closed:
                    helper.signal(signal.SIGKILL)
                    assert await helper.settle(deadline)
            assert owner.mutations.dispose_settled()
