import asyncio
import os
import socket
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from kora.conformance.controllers import duration_ns
from kora.conformance.lifetime import ParentLifetime
from kora.conformance.transport import LifetimeEndpoint
from kora.protocols.lifetime import (
    CleanupOutcome,
    ClosureReport,
    ProcessDomain,
    ProcessLaunch,
    ReservationRequest,
    ResourceKind,
    ResourceState,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.runner.filesystem import FilesystemError
from tests.conformance.lifetime_helpers import lifetime_failure_state
from tests.conformance.lifetime_support import (
    ControlledLifetimeClock,
    dispose_test_owner,
    installed_launcher,
    limits,
)


async def receive_feedback(
    feedback: socket.socket, running: asyncio.Task[ClosureReport]
) -> tuple[bytes, str | bytes]:
    receiving = asyncio.create_task(
        asyncio.get_running_loop().sock_recvfrom(feedback, 128)
    )
    try:
        await asyncio.wait((receiving, running), return_when=asyncio.FIRST_COMPLETED)
        if not receiving.done():
            report = running.result()
            raise AssertionError(f"child exited before feedback: {report.state}")
        return receiving.result()
    finally:
        if not receiving.done():
            receiving.cancel()
            try:
                await receiving
            except asyncio.CancelledError:
                assert receiving.cancelled()


async def run_probe(mode: str, root: str = "store") -> None:
    with TemporaryDirectory(prefix="kora-case-", dir="/tmp") as temporary:
        base = Path(temporary).resolve()
        clock = ControlledLifetimeClock()
        owner = ParentLifetime(
            clock=clock,
            limits=limits(),
            launcher=installed_launcher(base),
        )
        running: asyncio.Task[ClosureReport] | None = None
        try:
            with ExitStack() as resources:
                listener = resources.enter_context(
                    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                )
                address = str(base / "rpc")
                listener.bind(address)
                listener.listen(1)
                listener.setblocking(False)
                feedback = resources.enter_context(
                    socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                )
                feedback_address = str(base / "feedback")
                feedback.bind(feedback_address)
                feedback.setblocking(False)
                loop = asyncio.get_running_loop()
                tree = await owner.reserve(
                    ReservationRequest(
                        operation_id=new_operation_id(),
                        kind=ResourceKind.TEMPORARY_TREE,
                    )
                )
                acquired_tree = await owner.acquire(
                    tree.token, new_operation_id(), TemporaryTreeRequest()
                )
                await owner.acknowledge(
                    tree.token, acquired_tree.operation_id, acquired_tree.revision
                )
                child = await owner.reserve(
                    ReservationRequest(
                        operation_id=new_operation_id(),
                        kind=ResourceKind.PROCESS_DOMAIN,
                        parent_resource_id=tree.token.resource_id,
                    )
                )
                acquired = await owner.acquire(
                    child.token,
                    new_operation_id(),
                    ProcessLaunch(
                        executable=sys.executable,
                        argv=(
                            sys.executable,
                            "-I",
                            str(Path(__file__).with_name("storage_case_probe.py")),
                            address,
                            feedback_address,
                            mode,
                            root,
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
                endpoint, _ = await asyncio.wait_for(loop.sock_accept(listener), 5)
                running = asyncio.create_task(
                    LifetimeEndpoint(owner, child.token.resource_id, endpoint).run()
                )
                invocation = owner.mutations.directory
                process = owner.records[child.token.resource_id].process
                assert invocation is not None
                assert process is not None
                case_ids: list[str] = []
                cleaned_cases: list[str] = []
                terminal = b""

                async with asyncio.timeout(10):
                    while terminal not in {b"done", b"failed", b"blocked"}:
                        message, peer = await receive_feedback(feedback, running)
                        if message.startswith(b"case:"):
                            case_id = message.removeprefix(b"case:").decode()
                            case_ids.append(case_id)
                            assert case_id in owner.records
                            assert case_id in os.listdir(invocation.descriptor)
                        elif message == b"native":
                            assert case_ids[-1] in os.listdir(invocation.descriptor)
                            assert not process.closed
                        elif message == b"cleaned":
                            cleaned_cases.append(case_ids[-1])
                            assert case_ids[-1] in os.listdir(invocation.descriptor)
                            assert owner.records[case_ids[-1]].entry.state is not (
                                ResourceState.CLOSED
                            )
                        elif message == b"joining":
                            assert cleaned_cases[-1] == case_ids[-1]
                            assert case_ids[-1] in os.listdir(invocation.descriptor)
                            assert process.observe().live_members
                            if mode == "blocked":
                                terminal = b"blocked"
                            else:
                                await loop.sock_sendto(feedback, b"release", peer)
                        elif message == b"closed":
                            assert case_ids[-1] not in os.listdir(invocation.descriptor)
                            assert owner.records[case_ids[-1]].entry.state is (
                                ResourceState.CLOSED
                            )
                        else:
                            assert message in {b"done", b"failed"}
                            terminal = message
                    if terminal != b"done":
                        assert len(case_ids) == 1
                        assert case_ids[0] in os.listdir(invocation.descriptor)
                        assert process.observe().live_members
                        owner.request_close(None)
                        clock.advance(duration_ns(owner.limits.cooperative_seconds) + 1)
                    report = await running

                assert process.closed
                assert process.returncode is not None
                assert owner.inventory().retained_processes == 0
                assert owner.inventory().helpers.retained_helpers == 0
                if mode == "substitute":
                    assert report.state is CleanupOutcome.UNKNOWN
                    assert invocation.closed is False
                    original = owner.records[case_ids[0]].directory
                    assert original is not None
                    with pytest.raises(FilesystemError):
                        invocation.open_child(original.identity)
                    assert os.stat(
                        case_ids[0] + "/foreign", dir_fd=invocation.descriptor
                    ).st_size == len(b"preserve")
                    assert case_ids[0] + "-moved" in os.listdir(invocation.descriptor)
                else:
                    assert report.state is CleanupOutcome.CLOSED
                    assert invocation.closed
                    assert owner.inventory().retained_trees == 0
                if terminal == b"done":
                    assert len(case_ids) == 2
                    assert case_ids[0] != case_ids[1]
                    assert process.returncode == 0
        except (TimeoutError, AssertionError) as failure:
            error_path = base / "feedback.stderr"
            if error_path.exists():
                with error_path.open("rb") as errors:
                    failure.add_note(errors.read(8192).decode(errors="replace"))
            failure.add_note(lifetime_failure_state(owner))
            raise
        finally:
            try:
                if running is not None and not running.done():
                    running.cancel()
                    try:
                        await running
                    except asyncio.CancelledError:
                        assert running.cancelled()
            finally:
                await dispose_test_owner(owner, clock)


@pytest.mark.asyncio
@pytest.mark.parametrize("root", ["store", "alternate-store"])
async def test_storage_case_runner_preserves_configuration_and_loop(root: str) -> None:
    await run_probe("success", root)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native", "late", "blocked"])
async def test_storage_case_runner_waits_for_native_work(mode: str) -> None:
    await run_probe(mode)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "prepare",
        "bind",
        "construct",
        "exercise",
        "cleanup",
        "cancel",
        "task",
        "collected",
        "direct",
        "restore",
    ],
)
async def test_storage_case_runner_retains_failed_domain(mode: str) -> None:
    await run_probe(mode)


@pytest.mark.asyncio
async def test_storage_case_runner_refuses_substituted_directory() -> None:
    await run_probe("substitute")
