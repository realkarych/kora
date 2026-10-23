import asyncio
import gc
import os
import signal
import subprocess
import sys
import traceback
import weakref
from contextlib import ExitStack
from pathlib import Path

import pytest

from kora.conformance.io_helper import HelperError
from kora.conformance.journal import JournalEntry, JournalSnapshot
from kora.conformance.lifetime import LifetimeError, ParentLifetime
from kora.protocols.lifetime import (
    Acquisition,
    CleanupOutcome,
    CleanupReference,
    EnvironmentVariable,
    LifetimeReason,
    ProcessDomain,
    ProcessLaunch,
    ReservationRequest,
    ReservationToken,
    ResourceKind,
    ResourceState,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.filesystem import DirectoryAnchor
from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
from tests.conformance.test_lifetime_io_helper import entered_fifo
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.conformance.test_parent_lifetime import limits


def execution_input(resource_id: str, values: int) -> ProcessLaunch:
    return ProcessLaunch(
        executable=sys.executable,
        argv=(sys.executable, "-I", "-c", "pass"),
        environment=tuple(
            EnvironmentVariable(name=f"PAYLOAD{index}", value="x" * 60000)
            for index in range(values)
        ),
        working_directory_resource_id=resource_id,
        stdin=None,
        stdout=None,
        stderr=None,
        domain=ProcessDomain.INVOCATION,
    )


async def acquire_started(
    owner: ParentLifetime,
    token: ReservationToken,
    operation_id: str,
    request: ProcessLaunch,
    started: asyncio.Event,
) -> Acquisition:
    started.set()
    return await owner.acquire(token, operation_id, request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["bootstrap", "bootstrap_failure", "bootstrap_missing", "replay", "near"]
)
async def test_direct_execution_inputs_keep_aggregate_ownership_before_await(
    tmp_path: Path, case: str
) -> None:
    recovery = None
    if case == "bootstrap_missing":
        recovery = CleanupReference(invocation_id=new_operation_id())
    if case == "bootstrap_failure":
        previous = subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("lifetime_recovery_probe.py")),
                str(tmp_path),
                "tree",
            ],
            check=True,
            capture_output=True,
        )
        recovery = CleanupReference(invocation_id=previous.stdout.decode("ascii"))
        journal_path = next(tmp_path.rglob(recovery.invocation_id)) / "journal"
        journal = JournalSnapshot.model_validate_json(journal_path.read_bytes())
        entries = (
            *journal.entries,
            *(
                JournalEntry(
                    resource_id=new_operation_id(),
                    parent_resource_id=None,
                    kind=ResourceKind.TEMPORARY_TREE,
                    state=ResourceState.RESERVED,
                    revision=journal.revision,
                )
                for _ in range(56)
            ),
        )
        journal_path.write_bytes(
            journal.model_copy(update={"entries": entries}).model_dump_json().encode()
        )
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
            pause_at=1 if case.startswith("bootstrap") else 7,
        )
        clock = ControlledLifetimeClock()
        owner = ParentLifetime(
            limits=limits(), launcher=launcher, clock=clock, recovery=recovery
        )
        callers: list[asyncio.Task[Acquisition]] = []
        try:
            if case.startswith("bootstrap"):
                token = ReservationToken(
                    invocation_id=owner.invocation_id,
                    resource_id="d" * 32,
                    owner=owner.owner,
                    generation=owner.generation,
                )
                working_id = "e" * 32
            else:
                tree = await owner.reserve(
                    ReservationRequest(
                        operation_id=new_operation_id(),
                        kind=ResourceKind.TEMPORARY_TREE,
                    )
                )
                await owner.acquire(
                    tree.token, new_operation_id(), TemporaryTreeRequest()
                )
                process = await owner.reserve(
                    ReservationRequest(
                        operation_id=new_operation_id(),
                        kind=ResourceKind.PROCESS_DOMAIN,
                        parent_resource_id=tree.token.resource_id,
                    )
                )
                token = process.token
                working_id = tree.token.resource_id
            operation_id = new_operation_id()
            requests = [execution_input(working_id, 15 if case == "near" else 5)]
            started = asyncio.Event()
            callers.append(
                asyncio.create_task(
                    acquire_started(owner, token, operation_id, requests[0], started)
                )
            )
            await asyncio.wait_for(started.wait(), 5)
            actual_clock = SystemClock()
            deadline = actual_clock.monotonic_ns() + 5_000_000_000
            while not owner.mutations.helpers:
                assert actual_clock.monotonic_ns() < deadline
                if callers[0].done():
                    callers[0].result()
                    raise AssertionError("acquisition ended before helper frontier")
                wake: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(wake.set_result, None)
                await wake
            helper = owner.mutations.helpers[-1]
            await entered_fifo(helper.process.pid, reading, deadline, actual_clock)
            if case != "near":
                for _ in range(2):
                    request = execution_input(working_id, 5)
                    assert request == requests[0]
                    assert request is not requests[0]
                    assert (
                        request.environment[0].value
                        is not requests[0].environment[0].value
                    )
                    requests.append(request)
                    started = asyncio.Event()
                    callers.append(
                        asyncio.create_task(
                            acquire_started(
                                owner, token, operation_id, request, started
                            )
                        )
                    )
                    await asyncio.wait_for(started.wait(), 5)

            assert (
                900000
                <= owner.inventory().execution_bytes
                <= owner.limits.max_execution_snapshot_bytes
            )
            with pytest.raises(LifetimeError) as refused:
                await owner.acquire(token, operation_id, execution_input(working_id, 5))
            assert refused.value.reason is LifetimeReason.CAPACITY_EXCEEDED
            assert len(owner.ordinary_slots) == len(callers)
            if case != "near":
                callers[-1].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await callers[-1]
                assert 600000 <= owner.inventory().execution_bytes < 900000
            if case == "bootstrap":
                assert not owner.operations
                for caller in callers:
                    caller.cancel()
                for caller in callers:
                    with pytest.raises(asyncio.CancelledError):
                        await caller
                assert owner.inventory().execution_bytes == 0
            elif not case.startswith("bootstrap"):
                assert owner.inventory().pending_operations == 1
                owner.request_close(token.resource_id)
            writer = os.open(
                "stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=barrier.descriptor
            )
            os.close(writer)
            if case != "bootstrap":
                for caller in callers:
                    if not caller.cancelled():
                        with pytest.raises(LifetimeError):
                            await caller
                assert owner.inventory().execution_bytes == owner.execution_bytes
            if case in ("replay", "bootstrap_failure", "bootstrap_missing"):
                replays = tuple(weakref.ref(value) for value in requests[1:])
                callers.clear()
                caller = None
                request = None
                del requests[1:]
                gc.collect()
                assert all(reference() is None for reference in replays)
            if case in ("bootstrap_failure", "bootstrap_missing"):
                with pytest.raises(LifetimeError) as failed:
                    await asyncio.wait_for(owner.ready(), 5)
                assert failed.value.reason is (
                    LifetimeReason.CAPACITY_EXCEEDED
                    if case == "bootstrap_failure"
                    else LifetimeReason.JOURNAL_UNAVAILABLE
                )
                if case == "bootstrap_missing":
                    cause = failed.value.__cause__
                    assert isinstance(cause, HelperError)
                    retained_frames = len(tuple(traceback.walk_tb(cause.__traceback__)))
                    with pytest.raises(LifetimeError) as repeated:
                        await owner.ready()
                    assert repeated.value.__cause__ is cause
                    assert (
                        len(tuple(traceback.walk_tb(cause.__traceback__)))
                        == retained_frames
                    )
            else:
                await asyncio.wait_for(owner.ready(), 5)
            report = await asyncio.wait_for(owner.close(None), 5)

            assert report.state is (
                CleanupOutcome.UNKNOWN
                if recovery is not None
                else CleanupOutcome.CLOSED
            )
            assert not owner.ordinary_slots
            assert owner.inventory().pending_operations == 0
            assert helper.closed
            assert helper.process.returncode == 0
        finally:
            closing = owner.request_close(None)
            clock.advance(40_000_000_000)
            for helper in tuple(owner.mutations.helpers):
                if not helper.closed:
                    helper.signal(signal.SIGKILL)
                    await asyncio.wait_for(
                        helper.settle(clock.monotonic_ns() + 5_000_000_000), 5
                    )
            await asyncio.wait_for(asyncio.shield(closing), 5)
            for caller in callers:
                caller.cancel()
                try:
                    await caller
                except asyncio.CancelledError:
                    if not caller.cancelled():
                        raise
                except LifetimeError:
                    assert caller.done()
            if recovery is not None:
                assert owner.dispose_recovery_observers()
