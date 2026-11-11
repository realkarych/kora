import asyncio
import errno
import json
import os
import shutil
import signal
import sys
import threading
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import TypeAdapter

from kora.conformance.capture import Capture
from kora.plugins.runtime.local.runtime import (
    STOP_MATCHES,
    LocalRuntime,
    SweepRefused,
)
from kora.plugins.runtime.local.spawn import (
    Launcher,
    Launchers,
    Readiness,
    launch_deadline,
    read_readiness,
    runner_environment,
    spawn_runner,
)
from kora.plugins.runtime.local.supervisor import EXIT_FAILED, EXIT_REFUSED
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.evidence import EvidenceScope
from kora.runner.identity import (
    SCAN_NS,
    ArgumentMatch,
    IdentityObservation,
    IdentityState,
    NativeIdentityReader,
)
from kora.runner.processes import ExitAnchor
from kora.runner.registry import Fence, Retired, Running, Starting
from tests.plugins.runtime.local.frames_support import StepClock
from tests.support.local_peers import until
from tests.support.local_runners import (
    EXITING,
    NEVER_READY,
    carriers,
    clock_awaited,
    driven,
    exited_within,
    lingering_start,
    open_spawning,
    reap_runners,
    runner_pids,
    short_directory,
    starting_entry,
    submitted,
    supervised,
    written,
    zombie_children,
)
from tests.support.local_runners import runner_argv as runner_vector
from tests.support.local_runtime import (
    PATH_MARKER,
    READY_DEADLINE_SECONDS,
    MatchSteering,
    NudgedClock,
    SteeredIdentityReader,
    SteeredMatchReader,
    Steering,
    alternately_unknown,
    frozen,
    never_settling,
    open_descriptors,
    prepared,
    private_directory,
    protocol_error,
    recorded,
    restarted,
    runtime_context,
    state_root,
    unreadable,
)
from tests.support.retirement_evidence import retirement_failure_note

Steer = TypeVar("Steer")
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
        fenced = await supervised(runner_vector(runtime, root, "attempt-1", NONCE))
        await written(runtime, "attempt-2", starting_entry(sockets, NONCE), ctx)
        other = await supervised(runner_vector(runtime, root, "attempt-2", "b" * 32))
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
            outcome = await supervised(
                runner_vector(runtime, state, "attempt-1", NONCE)
            )
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


def retire_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        record for record in records if record.get("event") == "runtime.local.retire"
    ]


def sweep_records(capture: Capture, *secrets: str) -> list[tuple[str, int, int]]:
    records = capture.finish()
    for record in records:
        text = json.dumps(record)
        assert not any(secret in text for secret in secrets)
    swept = retire_records(records)
    for record in swept:
        expected = "INFO" if record.get("outcome") == "tombstoned" else "WARNING"
        assert record.get("level") == expected
    return [
        (
            str(record["outcome"]),
            int(str(record["matched"])),
            int(str(record["killed"])),
        )
        for record in swept
    ]


def decoys(argv: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    *head, attempt, domain, nonce = argv
    return (
        (*head, "attempt-other", domain, nonce),
        (*head[:-1], nonce, attempt, domain, head[-1]),
        (*argv, "extra"),
        (*head, attempt, domain),
    )


async def late_nonce_match_is_stopped_before_the_tombstone(
    root: Path, sockets: Path, capture: Capture
) -> None:
    ctx = runtime_context(capture)
    runtime, entry, argv, spawned = await lingering_start(root, sockets, ctx)
    try:
        with carriers(argv, *decoys(argv)) as (copy, *others):
            for pid in (spawned, copy.pid):
                os.kill(pid, signal.SIGSTOP)
            receipts = await asyncio.gather(
                runtime.retire("attempt-1", timedelta(seconds=10), ctx),
                runtime.retire("attempt-1", timedelta(seconds=10), ctx),
            )
            tombstone = await recorded(runtime, "attempt-1")
            copied = copy.wait(timeout=10)
            survivors = [other.poll() for other in others]
    finally:
        await runtime.close()
    assert isinstance(tombstone, Retired)
    assert receipts == [tombstone.receipt, tombstone.receipt]
    assert tombstone.receipt.scope is EvidenceScope.NO_RUNNER_STARTED
    assert entry.fence is None
    assert exited_within(spawned, 0)
    assert copied == -signal.SIGKILL
    assert survivors == [None, None, None, None]
    swept = sweep_records(capture, entry.nonce, str(root))
    assert {outcome for outcome, _, _ in swept} == {"tombstoned"}
    assert sum(killed for _, _, killed in swept) >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "confirmation", ["dead", "absent", "unknown", "live", "reused"]
)
async def test_ready_exit_watch_requires_fresh_disappearance(
    tmp_path: Path, kora_capture: Capture, confirmation: str
) -> None:
    descriptors = open_descriptors()
    zombies = zombie_children()
    root = state_root(tmp_path)
    ctx = runtime_context(kora_capture)
    reader = SteeredIdentityReader()
    entered = asyncio.Event()
    resumed = threading.Event()
    loop = asyncio.get_running_loop()
    live: IdentityObservation | None = None

    def held(observation: IdentityObservation, count: int) -> IdentityObservation:
        nonlocal live
        if count == 2:
            assert observation.process is not None
            assert not observation.process.dead
            live = observation
            loop.call_soon_threadsafe(entered.set)
            if not resumed.wait(10):
                raise TimeoutError("identity read was not resumed")
        if count == 3:
            if confirmation == "dead":
                assert observation.state is IdentityState.ABSENT or (
                    observation.process is not None and observation.process.dead
                )
                assert live is not None
                assert live.process is not None
                return replace(live, process=replace(live.process, dead=True))
            if confirmation == "absent":
                assert observation.state is IdentityState.ABSENT
                assert observation.process is None
            if confirmation == "unknown":
                return unreadable(observation, count)
            if confirmation in {"live", "reused"}:
                assert live is not None
                return live if confirmation == "live" else restarted(live, count)
        return observation

    anchors: list[ExitAnchor] = []
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets", identities=reader)
        try:
            with carriers(NEVER_READY) as (carrier,):
                reader.steered[carrier.pid] = held
                pinning = asyncio.create_task(
                    runtime.pin(
                        (carrier.pid,),
                        NEVER_READY,
                        anchors,
                        ctx.clock.monotonic_ns() + SECOND_NS,
                        ctx,
                    )
                )
                try:
                    await asyncio.wait_for(entered.wait(), 10)
                    assert len(anchors) == 1
                    carrier.kill()
                    await asyncio.wait_for(asyncio.shield(anchors[0].readiness), 10)
                    assert anchors[0].exited
                    assert not anchors[0].unknown
                    if confirmation == "absent":
                        assert carrier.wait(timeout=10) == -signal.SIGKILL

                    resumed.set()
                    if confirmation in {"dead", "absent"}:
                        pinned = await pinning
                        assert pinned == []
                    else:
                        with pytest.raises(SweepRefused, match="match_unreadable"):
                            await pinning

                    assert reader.reads[carrier.pid] == 3
                finally:
                    resumed.set()
                    if not pinning.done():
                        pinning.cancel()
                    await asyncio.gather(pinning, return_exceptions=True)
        finally:
            await runtime.close()
            for anchor in anchors:
                anchor.close()

    assert open_descriptors() == descriptors
    assert zombie_children() == zombies


async def assert_refused_then_recovered(
    runtime: LocalRuntime,
    entry: Starting,
    spawned: int,
    ctx: ProtocolContext,
    root: Path,
    capture: Capture,
    steered: dict[int, Steer],
    steering: Steer,
) -> None:
    try:
        steered[spawned] = steering
        refused = await protocol_error(
            runtime.retire("attempt-1", timedelta(seconds=10), ctx)
        )
        fenced = await recorded(runtime, "attempt-1")
        alive = not exited_within(spawned, 0)
        steered.clear()
        receipt = await runtime.retire("attempt-1", timedelta(seconds=10), ctx)
    finally:
        await runtime.close()
    assert (refused.code, refused.retryable, refused.details) == (
        "runner_stop_unconfirmed",
        True,
        {},
    )
    assert isinstance(fenced, Starting)
    assert fenced.fence is not None
    assert alive
    assert receipt.retirement_id == fenced.fence.retirement_id
    assert exited_within(spawned, 10)
    assert sweep_records(capture, entry.nonce, str(root)) == [
        ("match_unreadable", 1, 0),
        ("tombstoned", 1, 1),
    ]


async def unreadable_match_is_unconfirmed(
    root: Path, sockets: Path, capture: Capture
) -> None:
    ctx = runtime_context(capture)
    reader = SteeredIdentityReader()
    runtime, entry, _, spawned = await lingering_start(
        root, sockets, ctx, identities=reader
    )
    await assert_refused_then_recovered(
        runtime, entry, spawned, ctx, root, capture, reader.steered, unreadable
    )


async def persistent_unreadable_match_is_unconfirmed(
    root: Path, sockets: Path, capture: Capture
) -> None:
    clock = NudgedClock()
    ctx = runtime_context(capture, clock)
    matches = SteeredMatchReader()
    runtime, entry, _, spawned = await lingering_start(
        root,
        sockets,
        ctx,
        scanner=NativeIdentityReader(clock=clock, match_reader=matches),
    )
    await assert_refused_then_recovered(
        runtime,
        entry,
        spawned,
        ctx,
        root,
        capture,
        matches.steered,
        never_settling(clock, native_reads=1),
    )
    assert matches.reads[spawned] == 3


async def persistent_unreadable_bystander_refuses_and_keeps_fence(
    root: Path, sockets: Path, capture: Capture
) -> None:
    clock = NudgedClock()
    ctx = runtime_context(capture, clock)
    matches = SteeredMatchReader()
    runtime, entry, argv, spawned = await lingering_start(
        root,
        sockets,
        ctx,
        scanner=NativeIdentityReader(clock=clock, match_reader=matches),
    )
    try:
        with carriers(decoys(argv)[0]) as (bystander,):
            matches.steered[bystander.pid] = never_settling(clock)
            refused = await protocol_error(
                runtime.retire("attempt-1", timedelta(seconds=10), ctx)
            )
            reads = matches.reads[bystander.pid]
        fenced = await recorded(runtime, "attempt-1")
    finally:
        await runtime.close()
    assert (refused.code, refused.retryable, refused.details) == (
        "runner_stop_unconfirmed",
        True,
        {},
    )
    assert reads == 2
    assert isinstance(fenced, Starting)
    assert fenced.fence is not None
    assert not exited_within(spawned, 0)
    assert sweep_records(capture, entry.nonce, str(root)) == [
        ("listing_incomplete", 0, 0)
    ]
    swept = retire_records(capture.finish())
    assert swept[0]["scan_failure"] == "owner_or_argv_unknown"


async def queued_sweep_behind_held_reread(
    root: Path, sockets: Path, capture: Capture
) -> None:
    clock = NudgedClock()
    ctx = runtime_context(capture, clock)
    matches = SteeredMatchReader()
    entered = threading.Event()
    release = threading.Event()

    def expire_when_released(match: ArgumentMatch, count: int) -> ArgumentMatch:
        if count == 1:
            return ArgumentMatch.UNKNOWN
        entered.set()
        assert release.wait(READY_DEADLINE_SECONDS)
        clock.offset_ns += SCAN_NS
        return ArgumentMatch.UNKNOWN

    runtime, entry, argv, spawned = await lingering_start(
        root,
        sockets,
        ctx,
        scanner=NativeIdentityReader(clock=clock, match_reader=matches),
    )
    attempts = [f"attempt-{index}" for index in range(1, 6)]
    for attempt in attempts[1:]:
        await written(runtime, attempt, starting_entry(sockets, "b" * 32), ctx)
    grace = timedelta(seconds=10)
    try:
        with carriers(decoys(argv)[0]) as (bystander,):
            matches.steered[bystander.pid] = expire_when_released
            retiring = [
                asyncio.ensure_future(
                    protocol_error(runtime.retire(attempt, grace, ctx))
                )
                for attempt in attempts[:4]
            ]
            assert await asyncio.to_thread(entered.wait, READY_DEADLINE_SECONDS)
            await until(lambda: len(runtime.sweeps.jobs) == 4)
            fifth = await protocol_error(runtime.retire(attempts[4], grace, ctx))
            release.set()
            refused = await asyncio.gather(*retiring)
    finally:
        release.set()
        await runtime.close()
    assert fifth.code == "registry_busy"
    assert {error.code for error in refused} == {"runner_stop_unconfirmed"}
    assert not exited_within(spawned, 0)
    assert runtime.sweeps.jobs == set()
    assert (
        sweep_records(capture, entry.nonce, str(root))
        == [("listing_incomplete", 0, 0)] * 4
    )
    swept = retire_records(capture.finish())
    assert Counter(record["scan_failure"] for record in swept) == {
        "owner_or_argv_unknown": 1,
        "pid_listing_incomplete": 3,
    }


async def transient_unreadable_reads_do_not_refuse(
    root: Path, sockets: Path, capture: Capture
) -> None:
    ctx = runtime_context(capture)
    matches = SteeredMatchReader()
    runtime, entry, argv, spawned = await lingering_start(
        root,
        sockets,
        ctx,
        scanner=NativeIdentityReader(clock=ctx.clock, match_reader=matches),
    )
    try:
        with carriers(*decoys(argv)) as others:
            for pid in (spawned, *(other.pid for other in others)):
                matches.steered[pid] = alternately_unknown
            receipt = await runtime.retire("attempt-1", timedelta(seconds=10), ctx)
            survivors = [other.poll() for other in others]
            decoy_reads = [matches.reads[other.pid] for other in others]
    finally:
        await runtime.close()
    assert receipt.scope is EvidenceScope.NO_RUNNER_STARTED
    assert exited_within(spawned, 10)
    assert matches.reads[spawned] == 4
    assert survivors == [None, None, None, None]
    assert min(decoy_reads) >= 2
    assert sweep_records(capture, entry.nonce, str(root)) == [("tombstoned", 1, 1)]


async def live_match_at_the_grace_is_unconfirmed(
    root: Path, sockets: Path, capture: Capture
) -> None:
    ctx = runtime_context(capture)
    reader = SteeredIdentityReader()
    runtime, entry, _, spawned = await lingering_start(
        root, sockets, ctx, identities=reader
    )
    try:
        immediate = await protocol_error(runtime.retire("attempt-1", timedelta(0), ctx))
        untouched = not exited_within(spawned, 0)
        fenced = await recorded(runtime, "attempt-1")
        reader.steered[spawned] = frozen()
        receipt = await runtime.retire("attempt-1", timedelta(seconds=10), ctx)
    finally:
        await runtime.close()
    assert (immediate.code, immediate.details) == ("runner_stop_unconfirmed", {})
    assert untouched
    assert isinstance(fenced, Starting)
    assert fenced.fence is not None
    assert receipt.retirement_id == fenced.fence.retirement_id
    assert exited_within(spawned, 10)
    assert sweep_records(capture, entry.nonce, str(root)) == [
        ("survived_grace", 1, 0),
        ("tombstoned", 1, 1),
    ]


async def assert_not_signalled(
    root: Path,
    sockets: Path,
    capture: Capture,
    identity: Steering | None,
    match: MatchSteering | None,
) -> None:
    ctx = runtime_context(capture)
    reader = SteeredIdentityReader()
    matches = SteeredMatchReader()
    runtime, entry, _, spawned = await lingering_start(
        root,
        sockets,
        ctx,
        identities=reader,
        scanner=NativeIdentityReader(clock=ctx.clock, match_reader=matches),
    )
    try:
        if identity is not None:
            reader.steered[spawned] = identity
        if match is not None:
            matches.steered[spawned] = match
        receipt = await runtime.retire("attempt-1", timedelta(seconds=10), ctx)
        alive = not exited_within(spawned, 0)
    finally:
        await runtime.close()
    assert receipt.scope is EvidenceScope.NO_RUNNER_STARTED
    assert alive
    if match is not None:
        assert matches.reads[spawned] == 3
    assert sweep_records(capture, entry.nonce, str(root)) == [("tombstoned", 1, 0)]


def unknown_at_pinning(settled: ArgumentMatch | None) -> MatchSteering:
    def steer(match: ArgumentMatch, count: int) -> ArgumentMatch:
        if count == 2:
            return ArgumentMatch.UNKNOWN
        return match if settled is None or count == 1 else settled

    return steer


async def incomplete_listing_is_unconfirmed(
    root: Path, sockets: Path, capture: Capture
) -> None:
    ctx = runtime_context(capture)
    runtime, entry, _, spawned = await lingering_start(
        root,
        sockets,
        ctx,
        scanner=NativeIdentityReader(clock=ctx.clock, max_scan_entries=1),
    )
    try:
        refused = await protocol_error(
            runtime.retire("attempt-1", timedelta(seconds=10), ctx)
        )
        fenced = await recorded(runtime, "attempt-1")
    finally:
        await runtime.close()
    assert (refused.code, refused.details) == ("runner_stop_unconfirmed", {})
    assert isinstance(fenced, Starting)
    assert fenced.fence is not None
    assert not exited_within(spawned, 0)
    recovered = await open_spawning(root, ctx, sockets)
    with retirement_failure_note(capture, after_incomplete_listing=True):
        try:
            receipt = await recovered.retire("attempt-1", timedelta(seconds=10), ctx)
        finally:
            await recovered.close()
    assert receipt.retirement_id == fenced.fence.retirement_id
    assert exited_within(spawned, 10)
    assert sweep_records(capture, entry.nonce, str(root)) == [
        ("listing_incomplete", 0, 0),
        ("tombstoned", 1, 1),
    ]
    swept = retire_records(capture.finish())
    assert swept[0]["scan_failure"] == "pid_listing_incomplete"
    assert "scan_failure" not in swept[1]


async def too_many_matches_are_unconfirmed(
    root: Path, sockets: Path, capture: Capture
) -> None:
    ctx = runtime_context(capture)
    runtime, entry, argv, spawned = await lingering_start(root, sockets, ctx)
    try:
        with carriers(*(argv,) * STOP_MATCHES) as copies:
            refused = await protocol_error(
                runtime.retire("attempt-1", timedelta(seconds=10), ctx)
            )
            untouched = [copy.poll() for copy in copies]
        fenced = await recorded(runtime, "attempt-1")
    finally:
        await runtime.close()
    assert refused.code == "runner_stop_unconfirmed"
    assert untouched == [None] * STOP_MATCHES
    assert not exited_within(spawned, 0)
    assert isinstance(fenced, Starting)
    assert sweep_records(capture, entry.nonce, str(root)) == [
        ("too_many_matches", STOP_MATCHES + 1, 0)
    ]


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
    "late_nonce_match_is_stopped_before_the_tombstone": (
        late_nonce_match_is_stopped_before_the_tombstone
    ),
    "unreadable_match_is_unconfirmed": unreadable_match_is_unconfirmed,
    "live_match_at_the_grace_is_unconfirmed": live_match_at_the_grace_is_unconfirmed,
    "changed_identity_is_not_signalled": partial(
        assert_not_signalled, identity=restarted, match=None
    ),
    "dying_match_is_dropped": partial(
        assert_not_signalled,
        identity=None,
        match=unknown_at_pinning(ArgumentMatch.ABSENT),
    ),
    "identity_change_during_reread_is_not_signalled": partial(
        assert_not_signalled, identity=restarted, match=unknown_at_pinning(None)
    ),
    "persistent_unreadable_match_is_unconfirmed": (
        persistent_unreadable_match_is_unconfirmed
    ),
    "persistent_unreadable_bystander_refuses_and_keeps_fence": (
        persistent_unreadable_bystander_refuses_and_keeps_fence
    ),
    "queued_sweep_behind_held_reread": queued_sweep_behind_held_reread,
    "transient_unreadable_reads_do_not_refuse": (
        transient_unreadable_reads_do_not_refuse
    ),
    "incomplete_listing_is_unconfirmed": incomplete_listing_is_unconfirmed,
    "too_many_matches_are_unconfirmed": too_many_matches_are_unconfirmed,
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


@pytest.mark.asyncio
async def test_spawn_preparation_uses_original_readiness_deadline(
    tmp_path: Path,
) -> None:
    root = state_root(tmp_path)
    clock = StepClock()
    launchers = Launchers()
    cutoff = launch_deadline(clock)
    clock.advance(20 * SECOND_NS)
    task = asyncio.create_task(
        spawn_runner(
            (*NEVER_READY, str(root)),
            runner_environment(sys.platform, Path.home()),
            clock,
            launchers,
            deadline_ns=cutoff,
        )
    )
    try:
        await asyncio.wait_for(clock_awaited(clock, task), 5)
        waits = [at for at, future in clock.sleepers if not future.done()]
        clock.advance(10 * SECOND_NS)
        outcome = await asyncio.wait_for(task, 5)

        assert waits == [cutoff]
        assert outcome is Readiness.TIMEOUT
        assert launchers.unsettled == set()
        assert launchers.spawning == set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await launchers.drain(clock)
        reap_runners(root)
    assert runner_pids(root) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertainty_seconds", [0, 3])
async def test_spawn_readiness_respects_inherited_deadline(
    tmp_path: Path, kora_capture: Capture, uncertainty_seconds: int
) -> None:
    root = state_root(tmp_path)
    clock = StepClock()
    deadline = replace(
        gated(clock, 10), clock_uncertainty=timedelta(seconds=uncertainty_seconds)
    )
    ctx = runtime_context(kora_capture, clock, deadline)
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets", runner=NEVER_READY)
        work = submitted(
            runtime,
            "attempt-1",
            deadline=TypeAdapter(ExecutionDeadline).dump_python(deadline, mode="json"),
        )
        task = asyncio.create_task(protocol_error(runtime.submit(work, ctx)))
        try:
            await asyncio.wait_for(clock_awaited(clock, task), 5)
            waits = [at for at, future in clock.sleepers if not future.done()]
            clock.advance((10 - uncertainty_seconds) * SECOND_NS)
            outcome = await asyncio.wait_for(task, 5)

            assert waits == [clock.monotonic_ns()]
            assert outcome.code == "runner_starting"
            assert spawn_outcomes(kora_capture) == ["timeout"]
            assert runtime.launchers.unsettled == set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.close()
            reap_runners(root)


class SettlingClock(StepClock):
    async def sleep_until(self, deadline_ns: int) -> None:
        self.advance(2 * SECOND_NS)
        await super().sleep_until(deadline_ns)


@pytest.mark.asyncio
@pytest.mark.parametrize("expired_at_entry", [False, True])
async def test_spawn_timeout_keeps_launcher_reap_owned(
    tmp_path: Path, expired_at_entry: bool
) -> None:
    root = state_root(tmp_path)
    clock = SettlingClock()
    launchers = Launchers()
    descriptors = open_descriptors()
    zombies = zombie_children()
    environment = runner_environment(sys.platform, Path.home())
    owned = Launcher()
    pid = owned.child.start(
        lambda: os.posix_spawn(sys.executable, (*NEVER_READY, str(root)), environment)
    )
    launchers.unsettled.add(owned)
    cutoff = clock.monotonic_ns() + (0 if expired_at_entry else SECOND_NS)
    try:
        outcome = await asyncio.wait_for(
            spawn_runner(
                (*NEVER_READY, str(root)),
                environment,
                clock,
                launchers,
                deadline_ns=cutoff,
            ),
            5,
        )

        assert outcome is Readiness.TIMEOUT
        assert clock.offset_ns == 2 * SECOND_NS
        assert owned.reaped
        assert owned.closed
        assert launchers.spawning == launchers.settling == launchers.unsettled == set()
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        assert runner_pids(root) == ()
        assert zombie_children() == zombies
        assert open_descriptors() == descriptors
    finally:
        await launchers.drain(clock)
        reap_runners(root)


@pytest.mark.asyncio
async def test_expired_readiness_does_not_consume_ready_byte() -> None:
    clock = StepClock()
    cutoff = clock.monotonic_ns()
    read_end, write_end = os.pipe()
    os.write(write_end, b"R")
    try:
        outcome = await read_readiness(read_end, clock, cutoff)

        assert outcome is Readiness.TIMEOUT
        assert os.read(read_end, 1) == b"R"
    finally:
        os.close(write_end)
        os.close(read_end)
