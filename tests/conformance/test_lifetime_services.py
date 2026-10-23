import asyncio
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.lifetime import ParentLifetime
from kora.protocols.lifetime import (
    BorrowedServiceRequest,
    CleanupOutcome,
    ProcessDomain,
    ProcessLaunch,
    Reservation,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.runner.identity import IdentityState, NativeIdentityReader
from kora.runner.processes import ExitAnchor
from tests.conformance.test_parent_lifetime import limits


async def launch_service_process(
    owner: ParentLifetime, tree: Reservation, role: str, address: str, feedback: str
) -> Reservation:
    reservation = await owner.reserve(
        ReservationRequest(
            operation_id=new_operation_id(),
            kind=ResourceKind.PROCESS_DOMAIN,
            parent_resource_id=tree.token.resource_id,
        )
    )
    acquisition = await owner.acquire(
        reservation.token,
        new_operation_id(),
        ProcessLaunch(
            executable=sys.executable,
            argv=(
                sys.executable,
                "-I",
                str(
                    Path(__file__).resolve().parents[1] / "support/lifetime_service.py"
                ),
                role,
                address,
                feedback,
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
        reservation.token, acquisition.operation_id, acquisition.revision
    )
    return reservation


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["owned", "borrowed"])
async def test_conformance_fixture_resources_are_released_with_real_service_clients(
    tmp_path: Path, ownership: str
) -> None:
    owner = ParentLifetime(
        limits=limits(),
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
    external: subprocess.Popen[bytes] | None = None
    external_anchor = None
    with tempfile.TemporaryDirectory(prefix="ks-", dir="/tmp") as directory:
        address = str(Path(directory) / "service")
        feedback_address = str(Path(directory) / "feedback")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(feedback_address)
        listener.listen(1)
        listener.setblocking(False)
        if ownership == "borrowed":
            external = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(
                        Path(__file__).resolve().parents[1]
                        / "support/lifetime_service.py"
                    ),
                    "service",
                    address,
                    feedback_address,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            external_anchor = ExitAnchor(external.pid)
        else:
            await launch_service_process(
                owner, tree, "service", address, feedback_address
            )
        loop = asyncio.get_running_loop()
        feedback, _ = await asyncio.wait_for(loop.sock_accept(listener), 5)
        assert await asyncio.wait_for(loop.sock_recv(feedback, 1), 5) == b"r"
        client = await launch_service_process(
            owner, tree, "client", address, feedback_address
        )
        assert await asyncio.wait_for(loop.sock_recv(feedback, 1), 5) == b"c"
        scope = client.token.resource_id
        if ownership == "borrowed":
            reference = owner.bind_borrowed_service(client.token.resource_id)
            borrowed = await owner.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(),
                    kind=ResourceKind.BORROWED_SERVICE,
                    parent_resource_id=client.token.resource_id,
                )
            )
            await owner.acquire(
                borrowed.token,
                new_operation_id(),
                BorrowedServiceRequest(service_reference=reference, pre_existing=True),
            )
            borrowed_report = await owner.close(borrowed.token.resource_id)
            assert borrowed_report.state is CleanupOutcome.UNKNOWN
            client_process = owner.records[client.token.resource_id].process
            assert client_process is not None
            assert not client_process.closed

        clients_closed = await owner.close(scope)
        released = await asyncio.wait_for(loop.sock_recv(feedback, 1), 5)
        if external is not None:
            observed = NativeIdentityReader().read(external.pid)
            assert observed.state is IdentityState.PRESENT
            assert observed.process is not None
            assert not observed.process.dead
        report = await owner.close(None)

        assert clients_closed.state is CleanupOutcome.CLOSED
        assert released == b"e"
        assert report.state is CleanupOutcome.CLOSED
        assert owner.inventory().retained_processes == 0
        if external is not None and external_anchor is not None:
            assert not external_anchor.exited
            feedback.close()
            await external_anchor.wait(owner.deadline(), owner.clock)
            assert external.wait() == 0
            external_anchor.close()
        else:
            feedback.close()
        listener.close()
