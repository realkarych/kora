import asyncio
import errno
import os
import shutil
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.supervisor import EXIT_FAILED, EXIT_REFUSED
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.evidence import EvidenceScope
from kora.runner.registry import Fence, Running, Starting
from tests.plugins.runtime.local.frames_support import StepClock
from tests.support.local_runners import (
    EXITING,
    NEVER_READY,
    clock_awaited,
    driven,
    open_spawning,
    reap_runners,
    runner_argv,
    runner_pids,
    short_directory,
    starting_entry,
    submitted,
    supervised,
    written,
    zombie_children,
)
from tests.support.local_runtime import (
    PATH_MARKER,
    open_descriptors,
    prepared,
    private_directory,
    protocol_error,
    recorded,
    runtime_context,
    state_root,
)

__all__ = ["kora_capture"]

NONCE = "a" * 32
FENCE_ID = "retirement-fence"
SECOND_NS = 1_000_000_000


def spawn_outcomes(capture: Capture) -> list[str]:
    records = [
        record
        for record in capture.finish()
        if record.get("event") == "runtime.local.spawn"
    ]
    for record in records:
        expected = "INFO" if record.get("outcome") == "ready" else "WARNING"
        assert record.get("level") == expected
    return [str(record.get("outcome")) for record in records]


def gated(clock: StepClock, at_seconds: int) -> ExecutionDeadline:
    return ExecutionDeadline(
        at=clock.now() + timedelta(seconds=at_seconds),
        scope_kind="attempt",
        scope_id="attempt-1",
        clock_uncertainty=timedelta(0),
        grace=timedelta(seconds=30),
    )


async def retryable_until_the_deadline(
    root: Path, sockets: Path, capture: Capture
) -> None:
    clock = StepClock()
    deadline = gated(clock, 120)
    ctx = runtime_context(capture, clock, deadline)
    runtime = await open_spawning(root, ctx, sockets, runner=NEVER_READY)
    work = submitted(
        runtime,
        "attempt-1",
        deadline=TypeAdapter(ExecutionDeadline).dump_python(deadline, mode="json"),
    )
    try:
        timed_out = await driven(
            clock, protocol_error(runtime.submit(work, ctx)), SECOND_NS
        )
        again = await protocol_error(runtime.submit(work, ctx))
        looked = await protocol_error(runtime.lookup("attempt-1", ctx))
        clock.advance(120 * SECOND_NS)
        expired = await protocol_error(runtime.submit(work, ctx))
        entry = await recorded(runtime, "attempt-1")
    finally:
        await runtime.close()
    assert (timed_out.code, timed_out.retryable) == ("runner_starting", True)
    assert (again.code, again.retryable) == ("runner_starting", True)
    assert looked.code == "runner_starting"
    assert expired.code == "deadline_expired"
    assert isinstance(entry, Starting)
    assert entry.gate_closed
    assert spawn_outcomes(capture) == ["timeout"]


async def ended_runner_is_refused_at_once(
    root: Path, sockets: Path, capture: Capture
) -> None:
    clock = StepClock()
    ctx = runtime_context(capture, clock)
    runtime = await open_spawning(root, ctx, sockets, runner=EXITING)
    attempts = [f"attempt-{index}" for index in range(10)]
    try:
        refused = await asyncio.gather(
            *(
                protocol_error(runtime.submit(submitted(runtime, name), ctx))
                for name in attempts
            )
        )
        entries = [await recorded(runtime, name) for name in attempts]
    finally:
        await runtime.close()
    assert {(error.code, error.retryable) for error in refused} == {
        ("runner_starting", True)
    }
    assert all(isinstance(entry, Starting) for entry in entries)
    assert clock.offset_ns == 0
    assert spawn_outcomes(capture) == ["ended"] * 10


async def fenced_runner_exits_without_registering(
    root: Path, sockets: Path, capture: Capture
) -> None:
    ctx = runtime_context(capture)
    runtime = await open_spawning(root, ctx, sockets)
    fence = Fence(retirement_id=FENCE_ID, fenced_at=ctx.clock.now())
    entry = starting_entry(sockets, NONCE, fence)
    try:
        await written(runtime, "attempt-1", entry, ctx)
        fenced = await supervised(runner_argv(runtime, root, "attempt-1", NONCE))
        await written(runtime, "attempt-2", starting_entry(sockets, NONCE), ctx)
        other = await supervised(runner_argv(runtime, root, "attempt-2", "b" * 32))
        kept = [await recorded(runtime, name) for name in ("attempt-1", "attempt-2")]
    finally:
        await runtime.close()
    assert fenced == other
    assert (fenced.code, fenced.readiness) == (EXIT_REFUSED, b"")
    assert kept == [entry, starting_entry(sockets, NONCE)]
    assert os.listdir(sockets) == []


def fill(directory: Path) -> None:
    descriptor = os.open(
        directory / "filler", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    try:
        for size in (65536, 4096, 1):
            block = b"\0" * size
            while True:
                try:
                    os.write(descriptor, block)
                except OSError as error:
                    if error.errno != errno.ENOSPC:
                        raise
                    break
    finally:
        os.close(descriptor)


async def failed_registration_writes_nothing(
    root: Path, sockets: Path, capture: Capture
) -> None:
    small = private_directory(
        prepared("small-filesystem"), f"{PATH_MARKER}-{os.getpid()}"
    )
    try:
        state = private_directory(small, "state")
        ctx = runtime_context(capture)
        runtime = await open_spawning(state, ctx, sockets)
        entry = starting_entry(sockets, NONCE)
        try:
            await written(runtime, "attempt-1", entry, ctx)
            name = runtime.registry.key("attempt-1").name
            attempt = private_directory(private_directory(state, "attempts"), name)
            for log in ("runner.log", "runner.crash"):
                (attempt / log).touch(mode=0o600)
            fill(small)
            outcome = await supervised(runner_argv(runtime, state, "attempt-1", NONCE))
            kept = await recorded(runtime, "attempt-1")
            leftovers = sorted(os.listdir(state / "registry"))
        finally:
            await runtime.close()
        assert (outcome.code, outcome.readiness) == (EXIT_FAILED, b"")
        assert kept == entry
        assert not any(".tmp-" in leftover for leftover in leftovers)
        assert os.listdir(sockets) == []
    finally:
        shutil.rmtree(small)


async def retired_attempt_refuses_a_delayed_submit(
    root: Path, sockets: Path, capture: Capture
) -> None:
    ctx = runtime_context(capture)
    runtime = await open_spawning(root, ctx, sockets)
    try:
        receipt = await runtime.retire("attempt-1", timedelta(0), ctx)
        refused = await protocol_error(
            runtime.submit(submitted(runtime, "attempt-1"), ctx)
        )
    finally:
        await runtime.close()
    assert receipt.scope is EvidenceScope.NO_RUNNER_STARTED
    assert (refused.code, refused.retryable) == ("attempt_retired", False)
    assert runner_pids(root) == ()


async def running_runner_is_not_retired_yet(
    root: Path, sockets: Path, capture: Capture
) -> None:
    ctx = runtime_context(capture)
    runtime = await open_spawning(root, ctx, sockets)
    try:
        handle = await runtime.submit(submitted(runtime, "attempt-1"), ctx)
        refused = await protocol_error(runtime.retire("attempt-1", timedelta(0), ctx))
        entry = await recorded(runtime, "attempt-1")
    finally:
        await runtime.close()
    assert handle == runtime.registry.key("attempt-1").handle()
    assert (refused.code, refused.retryable) == ("retirement_unavailable", True)
    assert isinstance(entry, Running)
    assert spawn_outcomes(capture) == ["ready"]


async def cancelled_submits_leave_nothing(
    root: Path, sockets: Path, capture: Capture
) -> None:
    clock = StepClock()
    ctx = runtime_context(capture, clock)
    descriptors = open_descriptors()
    runtime = await open_spawning(root, ctx, sockets, runner=NEVER_READY)
    outcomes: list[BaseException | object] = []
    try:
        for index in range(3):
            work = submitted(runtime, f"attempt-{index}")
            clock.waiting.clear()
            submitting = asyncio.ensure_future(runtime.submit(work, ctx))
            await clock_awaited(clock, submitting)
            submitting.cancel()
            outcomes.extend(await asyncio.gather(submitting, return_exceptions=True))
    finally:
        await runtime.close()
    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
    assert runtime.launchers.unsettled == set()
    assert open_descriptors() == descriptors
    assert spawn_outcomes(capture) == ["cancelled"] * 3


LEGS: dict[str, Callable[[Path, Path, Capture], Awaitable[None]]] = {
    "retryable_until_the_deadline": retryable_until_the_deadline,
    "ended_runner_is_refused_at_once": ended_runner_is_refused_at_once,
    "fenced_runner_exits_without_registering": fenced_runner_exits_without_registering,
    "retired_attempt_refuses_a_delayed_submit": (
        retired_attempt_refuses_a_delayed_submit
    ),
    "running_runner_is_not_retired_yet": running_runner_is_not_retired_yet,
    "cancelled_submits_leave_nothing": cancelled_submits_leave_nothing,
}
PREPARED_LEGS: dict[str, Callable[[Path, Path, Capture], Awaitable[None]]] = {
    "failed_registration_writes_nothing": failed_registration_writes_nothing,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leg",
    [
        *sorted(LEGS),
        *(
            pytest.param(name, marks=pytest.mark.host_fixture)
            for name in sorted(PREPARED_LEGS)
        ),
    ],
)
async def test_starting_entry_resolves(
    tmp_path: Path, kora_capture: Capture, leg: str
) -> None:
    root = state_root(tmp_path)
    zombies = zombie_children()
    with short_directory() as base:
        sockets = base / "sockets"
        try:
            await {**LEGS, **PREPARED_LEGS}[leg](root, sockets, kora_capture)
        finally:
            reap_runners(root)

    assert runner_pids(root) == ()
    assert zombie_children() == zombies
