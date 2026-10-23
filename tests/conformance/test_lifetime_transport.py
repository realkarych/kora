import asyncio
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.lifetime import ParentLifetime
from kora.conformance.transport import LifetimeEndpoint
from kora.protocols.lifetime import (
    CleanupOutcome,
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
    with tempfile.TemporaryDirectory(prefix="kora-rpc-", dir="/tmp") as transport_root:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        address = str(Path(transport_root) / "rpc")
        feedback_address = str(Path(transport_root) / "feedback")
        feedback = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
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
        acquisition = await owner.acquire(child.token, new_operation_id(), launch)
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
            assert await asyncio.wait_for(loop.sock_recv(feedback, 1), 5) == b"p"
            deadline = owner.deadline()
            while not (
                owner.transport_execution_bytes
                if mode == "cancel_execution"
                else owner.transport_control_bytes
            ):
                assert owner.clock.monotonic_ns() < deadline
                ready: asyncio.Future[None] = loop.create_future()
                loop.call_soon(ready.set_result, None)
                await ready
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            report = await owner.close(None)
        else:
            report = await parent.run()

        listener.close()
        feedback.close()

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
