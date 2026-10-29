import asyncio
import os
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.plugins.runtime.local.spawn import (
    LAUNCHER_EXIT_NS,
    Launcher,
    Launchers,
    launcher_of,
)
from kora.plugins.runtime.local.supervisor import EXIT_REFUSED
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.time import SystemClock
from kora.runner.registry import (
    LOCK_DEADLINE_NS,
    LOCK_RETRY_NS,
    Entry,
    Fence,
    Transition,
    TransitionExecutor,
    bind_workspace,
)
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
    READY_DEADLINE_SECONDS,
    ConstructedIdentityReader,
    GatedIdentifiers,
    fill_boot_log,
    open_descriptors,
    open_runtime,
    protocol_error,
    recorded,
    runtime_context,
    state_root,
    stopped_holder,
)

__all__ = ["kora_capture"]

WAITERS = 8


async def bind(attempt: str, runtime: LocalRuntime, ctx: ProtocolContext) -> str:
    try:
        await runtime.workspace_target(attempt, ctx)
    except KoraProtocolError as error:
        return error.code
    return "accepted"


def runtime_threads() -> int:
    return sum(
        thread.name.startswith("kora-runtime-local") for thread in threading.enumerate()
    )


@pytest.mark.asyncio
async def test_registry_lock_is_bounded(tmp_path: Path, kora_capture: Capture) -> None:
    root = state_root(tmp_path)
    ctx = runtime_context(kora_capture)
    runtime = await open_runtime(root, ctx)
    await bind("attempt-held", runtime, ctx)
    lock = root / "registry" / f"{runtime.registry.key('attempt-held').name}.lock"

    try:
        with stopped_holder(lock):
            started = time.monotonic()
            waiters = [
                asyncio.create_task(bind("attempt-held", runtime, ctx))
                for _ in range(WAITERS)
            ]
            other = await bind("attempt-free", runtime, ctx)
            other_seconds = time.monotonic() - started
            looked = await recorded(runtime, "attempt-held")
            lookup_seconds = time.monotonic() - started
            codes = await asyncio.gather(*waiters)
            waited_seconds = time.monotonic() - started
    finally:
        await runtime.close()

    records = kora_capture.finish()
    timeouts = [
        record for record in records if record["event"] == "runtime.local.lock_timeout"
    ]
    assert other == "accepted"
    assert looked is not None
    assert codes == ["registry_busy"] * WAITERS
    assert other_seconds < 1.0
    assert lookup_seconds < 1.0
    assert 2.0 <= waited_seconds < 4.0
    assert len(timeouts) == WAITERS
    assert {record["operation"] for record in timeouts} == {"workspace_target"}
    assert all(PATH_MARKER not in str(record) for record in records)


async def lock_timeout(root: Path, capture: Capture) -> None:
    ctx = runtime_context(capture)
    runtime = await open_runtime(root, ctx)
    await bind("attempt-held", runtime, ctx)
    lock = root / "registry" / f"{runtime.registry.key('attempt-held').name}.lock"
    try:
        with stopped_holder(lock):
            refused = await bind("attempt-held", runtime, ctx)
    finally:
        await runtime.close()
    assert refused == "registry_busy"


async def admission_refusal(root: Path, capture: Capture) -> None:
    executor = TransitionExecutor(workers=1, admitted=1)
    started = threading.Event()
    gate = threading.Event()

    def hold() -> bool:
        started.set()
        return gate.wait(READY_DEADLINE_SECONDS)

    try:
        first = asyncio.create_task(executor.run(hold))
        assert await asyncio.to_thread(started.wait, READY_DEADLINE_SECONDS)
        refused = await asyncio.gather(
            executor.run(gate.is_set), return_exceptions=True
        )
        gate.set()
        held = await first
    finally:
        gate.set()
        await executor.close()
    assert held is True
    assert isinstance(refused[0], KoraProtocolError)
    assert (refused[0].code, refused[0].retryable) == ("registry_busy", True)


async def cancelled_caller(root: Path, capture: Capture) -> None:
    runtime = await open_runtime(root, runtime_context(capture))
    started = threading.Event()
    gate = threading.Event()
    key = runtime.registry.key("attempt-1")

    def decide(entry: Entry | None) -> Transition:
        started.set()
        gate.wait(READY_DEADLINE_SECONDS)
        return bind_workspace(entry, None, False)

    try:
        caller = asyncio.create_task(
            runtime.executor.run(lambda: runtime.registry.try_transition(key, decide))
        )
        assert await asyncio.to_thread(started.wait, READY_DEADLINE_SECONDS)
        caller.cancel()
        gate.set()
        outcome = await asyncio.gather(caller, return_exceptions=True)
        entry = await recorded(runtime, "attempt-1")
    finally:
        gate.set()
        await runtime.close()
    assert isinstance(outcome[0], asyncio.CancelledError)
    assert entry is not None
    assert entry.state == "unsubmitted"


async def open_failures(root: Path, capture: Capture) -> None:
    ctx = runtime_context(capture)
    unknown = ConstructedIdentityReader(pid_namespace=None)
    lineage = await protocol_error(open_runtime(root, ctx, unknown))
    unsafe = await protocol_error(open_runtime(root / "missing", ctx))
    fill_boot_log(root)
    full = await protocol_error(open_runtime(root, ctx))
    assert (lineage.code, unsafe.code, full.code) == (
        "lineage_unavailable",
        "state_root_unsafe",
        "boot_log_full",
    )


async def held_boot_lock(root: Path, capture: Capture) -> None:
    clock = StepClock()
    ctx = runtime_context(capture, clock)
    await (await open_runtime(root, ctx)).close()
    with stopped_holder(root / "boot.lock"):
        busy = asyncio.create_task(protocol_error(open_runtime(root, ctx)))
        await clock.waiting.wait()
        clock.advance(LOCK_DEADLINE_NS)
        refused = await busy
        waiting = asyncio.create_task(open_runtime(root, ctx))
        await clock.waiting.wait()
        waiting.cancel()
        cancelled = await asyncio.gather(waiting, return_exceptions=True)
    assert (refused.code, refused.retryable) == ("registry_busy", True)
    assert isinstance(cancelled[0], asyncio.CancelledError)


async def cancelled_open(root: Path, capture: Capture) -> None:
    identifiers = GatedIdentifiers()
    identifiers.armed = True
    opening = asyncio.create_task(
        open_runtime(root, runtime_context(capture), new_identifier=identifiers)
    )
    try:
        assert await asyncio.to_thread(identifiers.started.wait, READY_DEADLINE_SECONDS)
        opening.cancel()
    finally:
        identifiers.gate.set()
    outcome = await asyncio.gather(opening, return_exceptions=True)
    assert isinstance(outcome[0], asyncio.CancelledError)


async def held_transition(
    root: Path, capture: Capture
) -> tuple[LocalRuntime, GatedIdentifiers, asyncio.Task[str]]:
    identifiers = GatedIdentifiers()
    ctx = runtime_context(capture)
    runtime = await open_runtime(root, ctx, new_identifier=identifiers)
    identifiers.armed = True
    binding = asyncio.create_task(bind("attempt-1", runtime, ctx))
    assert await asyncio.to_thread(identifiers.started.wait, READY_DEADLINE_SECONDS)
    return runtime, identifiers, binding


async def reopened_entry(root: Path, capture: Capture) -> str | None:
    runtime = await open_runtime(root, runtime_context(capture))
    try:
        entry = await recorded(runtime, "attempt-1")
    finally:
        await runtime.close()
    return None if entry is None else entry.state


async def close_drains_transitions(root: Path, capture: Capture) -> None:
    runtime, identifiers, binding = await held_transition(root, capture)
    try:
        closing = asyncio.create_task(runtime.close())
        done, _ = await asyncio.wait({closing}, timeout=0)
        late = await bind("attempt-2", runtime, runtime_context(capture))
    finally:
        identifiers.gate.set()
    await closing
    assert done == set()
    assert await binding == "accepted"
    assert late == "runtime_closed"
    assert await reopened_entry(root, capture) == "unsubmitted"


async def concurrent_close(root: Path, capture: Capture) -> None:
    runtime, identifiers, binding = await held_transition(root, capture)
    try:
        first = asyncio.create_task(runtime.close())
        second = asyncio.create_task(runtime.close())
        await asyncio.wait({first, second}, timeout=0)
        first.cancel()
        cancelled = await asyncio.gather(first, return_exceptions=True)
        second_pending = not second.done()
    finally:
        identifiers.gate.set()
    await second
    await runtime.close()
    assert isinstance(cancelled[0], asyncio.CancelledError)
    assert second_pending
    assert await binding == "accepted"
    assert await reopened_entry(root, capture) == "unsubmitted"


async def retrying_caller_sees_close(root: Path, capture: Capture) -> None:
    clock = StepClock()
    ctx = runtime_context(capture, clock)
    runtime = await open_runtime(root, ctx)
    await bind("attempt-held", runtime, ctx)
    lock = root / "registry" / f"{runtime.registry.key('attempt-held').name}.lock"
    try:
        with stopped_holder(lock):
            retrying = asyncio.create_task(bind("attempt-held", runtime, ctx))
            await clock.waiting.wait()
            closing = asyncio.create_task(runtime.close())
            await asyncio.wait({closing}, timeout=0)
            clock.advance(LOCK_RETRY_NS)
            refused = await retrying
    finally:
        await runtime.close()
    assert refused == "runtime_closed"


def assert_no_runner(root: Path) -> None:
    reap_runners(root)
    assert runner_pids(root) == ()


async def spawned_runner_is_released(root: Path, capture: Capture) -> None:
    ctx = runtime_context(capture)
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets")
        try:
            await runtime.submit(submitted(runtime, "attempt-1"), ctx)
        finally:
            await runtime.close()
        killed = reap_runners(root)
    assert len(killed) == 1
    assert_no_runner(root)


async def readiness_timeout_is_released(root: Path, capture: Capture) -> None:
    clock = StepClock()
    ctx = runtime_context(capture, clock)
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets", runner=NEVER_READY)
        try:
            work = submitted(runtime, "attempt-1")
            refused = await driven(
                clock, protocol_error(runtime.submit(work, ctx)), 1_000_000_000
            )
        finally:
            await runtime.close()
    assert refused.code == "runner_starting"
    assert runtime.launchers.unsettled == set()
    assert_no_runner(root)


async def cancelled_readiness_is_released(root: Path, capture: Capture) -> None:
    clock = StepClock()
    ctx = runtime_context(capture, clock)
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets", runner=NEVER_READY)
        try:
            submitting = asyncio.ensure_future(
                runtime.submit(submitted(runtime, "attempt-1"), ctx)
            )
            await clock_awaited(clock, submitting)
            submitting.cancel()
            cancelled = await asyncio.gather(submitting, return_exceptions=True)
        finally:
            await runtime.close()
    assert isinstance(cancelled[0], asyncio.CancelledError)
    assert runtime.launchers.settling == set()
    assert_no_runner(root)


async def unsettled_launcher_is_retried(root: Path, capture: Capture) -> None:
    clock = SystemClock()
    launchers = Launchers(bound_ns=0)
    launcher = launcher_of(os.posix_spawn(NEVER_READY[0], list(NEVER_READY), {}))
    await launchers.settled(launcher, clock, kill=False)
    unsettled = set(launchers.unsettled)
    launchers.bound_ns = LAUNCHER_EXIT_NS
    await launchers.drain(clock)

    assert unsettled == {launcher}
    assert launchers.unsettled == set()
    assert launcher.reaped
    assert launcher.closed


async def unwatched_launcher_is_killed_and_reaped(root: Path, capture: Capture) -> None:
    clock = SystemClock()
    launchers = Launchers()
    unwatched = Launcher(os.posix_spawn(NEVER_READY[0], list(NEVER_READY), {}), None)
    await launchers.settled(unwatched, clock, kill=True)
    collected = os.posix_spawn(EXITING[0], list(EXITING), {})
    os.waitpid(collected, 0)
    elsewhere = Launcher(collected, None)
    await launchers.settled(elsewhere, clock, kill=False)

    assert unwatched.reaped
    assert elsewhere.reaped
    assert launchers.unsettled == set()


async def close_refuses_a_spawn_after_its_transition(
    root: Path, capture: Capture
) -> None:
    ctx = runtime_context(capture)
    identifiers = GatedIdentifiers()
    with short_directory() as base:
        runtime = await open_spawning(
            root, ctx, base / "sockets", new_identifier=identifiers
        )
        work = submitted(runtime, "attempt-1")
        identifiers.armed = True
        submitting = asyncio.create_task(protocol_error(runtime.submit(work, ctx)))
        try:
            assert await asyncio.to_thread(
                identifiers.started.wait, READY_DEADLINE_SECONDS
            )
            closing = asyncio.create_task(runtime.close())
            done, _ = await asyncio.wait({closing}, timeout=0)
        finally:
            identifiers.gate.set()
        refused = await submitting
        await closing
    assert done == set()
    assert refused.code == "runtime_closed"
    assert runtime.launchers.spawning == set()
    assert runner_pids(root) == ()


async def late_registration_is_released(root: Path, capture: Capture) -> None:
    ctx = runtime_context(capture)
    with short_directory() as base:
        sockets = base / "sockets"
        runtime = await open_spawning(root, ctx, sockets)
        fence = Fence(retirement_id="retirement-1", fenced_at=ctx.clock.now())
        try:
            await written(
                runtime, "attempt-1", starting_entry(sockets, "a" * 32, fence), ctx
            )
            argv = runner_argv(runtime, root, "attempt-1", "a" * 32)
            outcome = await supervised(argv)
        finally:
            await runtime.close()
        leftovers = os.listdir(sockets)
    assert outcome.code == EXIT_REFUSED
    assert leftovers == []
    assert_no_runner(root)


LEGS: dict[str, Callable[[Path, Capture], Awaitable[None]]] = {
    "spawned_runner_is_released": spawned_runner_is_released,
    "readiness_timeout_is_released": readiness_timeout_is_released,
    "cancelled_readiness_is_released": cancelled_readiness_is_released,
    "unsettled_launcher_is_retried": unsettled_launcher_is_retried,
    "unwatched_launcher_is_killed_and_reaped": unwatched_launcher_is_killed_and_reaped,
    "late_registration_is_released": late_registration_is_released,
    "close_refuses_a_spawn_after_its_transition": (
        close_refuses_a_spawn_after_its_transition
    ),
    "lock_timeout": lock_timeout,
    "admission_refusal": admission_refusal,
    "cancelled_caller": cancelled_caller,
    "cancelled_open": cancelled_open,
    "open_failures": open_failures,
    "held_boot_lock": held_boot_lock,
    "close_drains_transitions": close_drains_transitions,
    "concurrent_close": concurrent_close,
    "retrying_caller_sees_close": retrying_caller_sees_close,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", sorted(LEGS))
async def test_local_runtime_releases_resources(
    tmp_path: Path, kora_capture: Capture, leg: str
) -> None:
    descriptors = open_descriptors()
    threads = runtime_threads()
    zombies = zombie_children()

    await LEGS[leg](state_root(tmp_path), kora_capture)

    assert open_descriptors() == descriptors
    assert runtime_threads() == threads == 0
    assert zombie_children() == zombies
