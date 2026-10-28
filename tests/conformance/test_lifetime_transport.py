import asyncio
import signal
import socket
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

import pytest

from kora.conformance.controllers import duration_ns
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
from tests.conformance.lifetime_helpers import lifetime_failure_state, unread_bytes
from tests.conformance.lifetime_support import (
    ControlledLifetimeClock,
    dispose_test_owner,
    installed_launcher,
)
from tests.support.event_loop import turn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "eof",
        "terminal",
        "idle",
        "cancel_frame",
        "cancel_execution",
        "header_partial",
        "header_oversized",
        "header_malformed",
        "header_replayed",
        "header_verb",
    ],
)
async def test_conformance_fixture_resources_are_released_through_real_client_process(
    tmp_path: Path, mode: str
) -> None:
    with ExitStack() as resources:
        clock = ControlledLifetimeClock()
        owner = ParentLifetime(
            clock=clock,
            limits=LifetimeLimits(
                cooperative_seconds=0.5,
                term_seconds=0.1,
                kill_observation_seconds=2.0,
                drain_seconds=1.0,
            ),
            launcher=installed_launcher(tmp_path),
        )
        running: asyncio.Task[ClosureReport] | None = None
        try:
            async with asyncio.timeout(10):
                tree = await owner.reserve(
                    ReservationRequest(
                        operation_id=new_operation_id(),
                        kind=ResourceKind.TEMPORARY_TREE,
                    )
                )
                await owner.acquire(
                    tree.token, new_operation_id(), TemporaryTreeRequest()
                )
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
                    closing_peer: str | bytes | None = None
                    if mode in {"eof", "terminal", "idle"}:
                        running = asyncio.create_task(parent.run())
                        loop = asyncio.get_running_loop()
                        if mode == "idle":
                            marker, peer = await loop.sock_recvfrom(feedback, 1)
                            assert marker == b"r"
                            clock.advance(
                                2 * duration_ns(owner.limits.acquisition_seconds)
                            )
                            await loop.sock_sendto(feedback, b"s", peer)
                            assert await loop.sock_recv(feedback, 1) == b"p"
                            assert owner.admission_open
                        marker, closing_peer = await loop.sock_recvfrom(feedback, 1)
                        assert marker == b"c"

                    frontier_deadline = asyncio.get_running_loop().time() + 5
                    async with asyncio.timeout_at(frontier_deadline):
                        if closing_peer is not None:
                            assert running is not None
                            loop = asyncio.get_running_loop()
                            await loop.sock_sendto(feedback, b"s", closing_peer)
                            report = await running
                        elif mode.startswith("header_"):
                            running = asyncio.create_task(parent.run())
                            loop = asyncio.get_running_loop()
                            marker, peer = await asyncio.wait_for(
                                loop.sock_recvfrom(feedback, 1),
                                max(0, frontier_deadline - loop.time()),
                            )
                            assert marker == b"r"
                            retained = parent.control_bytes
                            resource_count = len(owner.records)
                            original_interval = duration_ns(
                                owner.limits.acquisition_seconds
                            )
                            if mode == "header_partial":
                                clock.advance(original_interval // 2)
                            original_deadline = clock.monotonic_ns() + original_interval
                            await loop.sock_sendto(feedback, b"s", peer)
                            assert (
                                await asyncio.wait_for(
                                    loop.sock_recv(feedback, 1),
                                    max(0, frontier_deadline - loop.time()),
                                )
                                == b"p"
                            )
                            if mode == "header_partial":
                                while not running.done() and (
                                    unread_bytes(endpoint) or not clock.waiters
                                ):
                                    assert loop.time() < frontier_deadline
                                    await turn()
                                assert not running.done(), running.result()
                                assert not parent.pending
                                assert not owner.ordinary_slots
                                assert parent.control_bytes == retained
                                armed = set(clock.waiters.values())
                                assert armed == {original_deadline}
                                clock.advance(
                                    original_deadline - clock.monotonic_ns() + 1
                                )
                            report = await asyncio.wait_for(
                                running, max(0, frontier_deadline - loop.time())
                            )
                            assert len(owner.records) == resource_count
                        elif mode.startswith("cancel"):
                            running = asyncio.create_task(parent.run())
                            loop = asyncio.get_running_loop()
                            phases = (
                                (b"r", b"d", b"p")
                                if mode == "cancel_frame"
                                else (b"r", b"d", b"s", b"p")
                            )
                            for phase in phases:
                                assert (
                                    await asyncio.wait_for(
                                        loop.sock_recv(feedback, 1),
                                        max(0, frontier_deadline - loop.time()),
                                    )
                                    == phase
                                )
                            while not (
                                owner.transport_execution_bytes
                                if mode == "cancel_execution"
                                else owner.transport_control_bytes
                                - ENDPOINT_CONTROL_BYTES
                            ):
                                assert loop.time() < frontier_deadline
                                await turn()
                            assert not parent.pending
                            assert any(
                                slot.wire and slot.task is None
                                for slot in owner.ordinary_slots
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
        except TimeoutError as failure:
            failure.add_note(lifetime_failure_state(owner))
            raise
        finally:
            try:
                if running is not None:
                    running.cancel()
                    try:
                        await running
                    except asyncio.CancelledError:
                        if not running.cancelled():
                            raise
            finally:
                await dispose_test_owner(owner, clock)
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
