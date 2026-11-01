import asyncio
import gc
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from math import isfinite
from pathlib import Path

import pytest

from kora.conformance.capture import Capture, CaptureOverflow, CaptureTimeout
from kora.conformance.common import kora_capture
from kora.ir.json import parse_json_object
from kora.plugins.runtime.local.runtime import LocalRuntime, SweepOutcome
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.time import SystemClock
from kora.runner.identity import NativeIdentityReader, ProcessScanFailure
from kora.runner.peers import CredentialSource, DarwinSource, PeerSource, kernel_source
from kora.runner.registry import Registry, bind_workspace
from tests.measurements.harness import OUTPUT_VARIABLE, REVISION_VARIABLE
from tests.support.local_peers import (
    asked_status,
    client,
    identity_of,
    judging_source,
    served,
)
from tests.support.local_runners import (
    LINGERING,
    PS,
    open_spawning,
    reap_runners,
    runner_pids,
    short_directory,
    submitted,
)
from tests.support.local_runtime import (
    open_descriptors,
    open_runtime,
    private_directory,
    protocol_error,
    runtime_context,
    state_root,
    stopped_holder,
)

__all__ = ["kora_capture"]

REPEATS = 50
WARM_UP = 5
LARGE_REGISTRY = 10000
CONCURRENCY = 10
SPAWN_REPEATS = 10
RETENTION_CYCLES = 20
RETENTION_INTERVAL = 5
SWEEP_GRACE = timedelta(seconds=10)
ATTEST_CONNECTIONS = 100
ATTEST_WARM_UP = 10
ATTEST_CONCURRENCY = 8


def summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "median_ms": statistics.median(ordered) * 1000,
        "p95_ms": ordered[max(0, int(len(ordered) * 0.95) - 1)] * 1000,
        "max_ms": ordered[-1] * 1000,
        "count": float(len(ordered)),
    }


def held(registry: Registry, prefix: str, count: int) -> list[float]:
    samples: list[float] = []
    for index in range(count):
        key = registry.key(f"{prefix}-{index}")
        started = time.perf_counter()
        registry.try_transition(key, lambda entry: bind_workspace(entry, None, False))
        samples.append(time.perf_counter() - started)
    return samples


def populate(directory: Path, count: int) -> None:
    for index in range(count):
        descriptor = os.open(
            directory / f"{index:032x}.lock", os.O_WRONLY | os.O_CREAT, 0o600
        )
        os.close(descriptor)


async def timed(operation: Awaitable[object]) -> float:
    started = time.perf_counter()
    await operation
    return time.perf_counter() - started


async def loop_lag(stop: asyncio.Event, lags: list[float]) -> None:
    while not stop.is_set():
        expected = time.perf_counter() + 0.001
        await asyncio.sleep(0.001)
        lags.append(max(0.0, time.perf_counter() - expected))


async def with_lag(
    work: Callable[[], Awaitable[list[float]]],
) -> tuple[list[float], list[float]]:
    stop = asyncio.Event()
    lags: list[float] = []
    ticker = asyncio.create_task(loop_lag(stop, lags))
    try:
        values = await work()
    finally:
        stop.set()
        await ticker
    return values, lags


async def bound(runtime: LocalRuntime, attempt: str, ctx: ProtocolContext) -> None:
    await runtime.workspace_target(attempt, ctx)


async def retired(runtime: LocalRuntime, attempt: str, ctx: ProtocolContext) -> None:
    await runtime.retire(attempt, timedelta(0), ctx)


@pytest.mark.measurement
@pytest.mark.asyncio
async def test_local_runtime_lock_contention(
    tmp_path: Path, kora_capture: Capture
) -> None:
    root = state_root(tmp_path)
    ctx = runtime_context(kora_capture)
    descriptors = open_descriptors()
    opened_at = time.perf_counter()
    runtime = await open_runtime(root, ctx)
    open_seconds = time.perf_counter() - opened_at
    registry = runtime.registry
    try:
        held(registry, "warm", WARM_UP)
        hold = held(registry, "hold", REPEATS)
        populate(root / "registry", LARGE_REGISTRY)
        large_hold = held(registry, "large", REPEATS)

        async def uncontended() -> list[float]:
            return [
                await timed(bound(runtime, f"single-{index}", ctx))
                for index in range(REPEATS)
            ]

        async def same_attempt_targets() -> list[float]:
            return list(
                await asyncio.gather(
                    *(
                        timed(bound(runtime, "contended", ctx))
                        for _ in range(CONCURRENCY)
                    )
                )
            )

        async def same_attempt_retires() -> list[float]:
            return list(
                await asyncio.gather(
                    *(
                        timed(retired(runtime, "retired", ctx))
                        for _ in range(CONCURRENCY)
                    )
                )
            )

        transitions, transition_lag = await with_lag(uncontended)
        targets, target_lag = await with_lag(same_attempt_targets)
        retires, retire_lag = await with_lag(same_attempt_retires)
        lock = root / "registry" / f"{runtime.registry.key('stopped').name}.lock"
        await bound(runtime, "stopped", ctx)
        with stopped_holder(lock):
            stopped_started = time.perf_counter()
            refused = await asyncio.gather(
                bound(runtime, "stopped", ctx), return_exceptions=True
            )
            stopped_wait = time.perf_counter() - stopped_started
    finally:
        await runtime.close()
    with short_directory() as base:
        sweeps = await swept_under_contention(
            state_root(private_directory(tmp_path, "sweeps")), base, ctx
        )
    descriptor_delta = open_descriptors() - descriptors
    swept = [
        float(str(record["waited_ms"])) / 1000
        for record in kora_capture.finish()
        if record.get("event") == "runtime.local.retire"
        and record.get("outcome") == "tombstoned"
    ]

    report = {
        "workload": "local-runtime-lock-contention",
        "revision": os.environ.get(REVISION_VARIABLE, "local"),
        "platform": f"{sys.platform}-{platform.machine()}",
        "python": platform.python_version(),
        "repeats": REPEATS,
        "concurrency": CONCURRENCY,
        "open_with_boot_lock": summary([open_seconds]),
        "lock_hold_read_compare_write": summary(hold),
        "lock_hold_with_large_registry": summary(large_hold),
        "large_registry_entries": LARGE_REGISTRY,
        "hold_measured_on_the_calling_thread": True,
        "uncontended_transition_including_queue": summary(transitions),
        "same_attempt_workspace_target": summary(targets),
        "same_attempt_retire": summary(retires),
        "loop_lag_during_transitions": summary(transition_lag or [0.0]),
        "loop_lag_during_contended_targets": summary(target_lag or [0.0]),
        "loop_lag_during_contended_retires": summary(retire_lag or [0.0]),
        "stopped_holder_wait": summary([stopped_wait]),
        "stopped_holder_outcome": type(refused[0]).__name__,
        **sweeps,
        "starting_retire_event_waited": summary(swept or [0.0]),
        "open_descriptor_delta": descriptor_delta,
    }
    published(report)
    assert report["open_descriptor_delta"] == 0


async def sweep_retire(
    runtime: LocalRuntime, attempt: str, ctx: ProtocolContext
) -> float | str:
    started = time.perf_counter()
    try:
        await runtime.retire(attempt, SWEEP_GRACE, ctx)
    except KoraProtocolError as error:
        return error.code
    return time.perf_counter() - started


def processes(*selection: str) -> int:
    listing = subprocess.run(
        [PS, *selection, "-o", "pid="],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return len(listing.stdout.splitlines())


async def swept_under_contention(
    root: Path, base: Path, ctx: ProtocolContext
) -> dict[str, object]:
    runtime = await open_spawning(root, ctx, base / "sockets", runner=LINGERING)
    attempts = [f"swept-{index}" for index in range(CONCURRENCY)]
    try:
        for attempt in attempts:
            await cycle(runtime, attempt, ctx)
        host = processes("-A")
        owned = processes("-U", str(os.getuid()))

        async def sweeping() -> list[float]:
            retiring = asyncio.gather(
                *(sweep_retire(runtime, attempt, ctx) for attempt in attempts)
            )
            targets = [
                await timed(bound(runtime, f"beside-{index}", ctx))
                for index in range(CONCURRENCY)
            ]
            outcomes.extend(await retiring)
            return targets

        outcomes: list[float | str] = []
        targets, lag = await with_lag(sweeping)
        retried = [await sweep_retire(runtime, attempt, ctx) for attempt in attempts]
    finally:
        await runtime.close()
        reap_runners(root)
    completed = [value for value in outcomes if isinstance(value, float)]
    return {
        "starting_retire_completed": summary(completed or [0.0]),
        "starting_retire_refusals": sorted(
            str(value) for value in outcomes if isinstance(value, str)
        ),
        "starting_retire_retried_refusals": sorted(
            str(value) for value in retried if isinstance(value, str)
        ),
        "workspace_target_during_sweeps_including_queue": summary(targets),
        "loop_lag_during_sweeps": summary(lag or [0.0]),
        "host_processes_during_sweeps": host,
        "same_uid_processes_during_sweeps": owned,
    }


def published(report: Mapping[str, object]) -> None:
    output = os.environ.get(OUTPUT_VARIABLE)
    if output is not None:
        path = Path(output)
        reports: dict[str, object] = {}
        if path.exists():
            reports.update(parse_json_object(json.loads(path.read_text())))
        reports[str(report["workload"])] = report
        path.write_text(json.dumps(reports, indent=2, sort_keys=True))
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")


def resident_kib(pid: int) -> float:
    listing = subprocess.run(
        [PS, "-o", "rss=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return float(listing.stdout.strip())


def floor_seconds() -> float:
    started = time.perf_counter()
    pid = os.posix_spawn(sys.executable, [sys.executable, "-I", "-c", "pass"], {})
    os.waitpid(pid, 0)
    return time.perf_counter() - started


async def submitted_in(
    runtime: LocalRuntime, attempt: str, ctx: ProtocolContext
) -> None:
    await runtime.submit(submitted(runtime, attempt), ctx)


async def cycle(runtime: LocalRuntime, attempt: str, ctx: ProtocolContext) -> None:
    refused = await protocol_error(submitted_in(runtime, attempt, ctx))
    assert refused.code == "runner_starting"


async def retired_cycle(
    runtime: LocalRuntime, attempt: str, ctx: ProtocolContext
) -> None:
    await cycle(runtime, attempt, ctx)
    await runtime.retire(attempt, SWEEP_GRACE, ctx)


def component_bytes(after: tracemalloc.Snapshot, before: tracemalloc.Snapshot) -> int:
    return sum(
        statistic.size_diff
        for statistic in after.compare_to(before, "filename")
        if "kora/plugins/runtime/local" in statistic.traceback[0].filename
        or "kora/runner" in statistic.traceback[0].filename
        or "kora/executor/launcher" in statistic.traceback[0].filename
    )


def last_retirement_observation(
    records: list[dict[str, object]],
) -> dict[str, object] | None:
    observed = next(
        (
            record
            for record in reversed(records)
            if record.get("event") == "runtime.local.retire"
        ),
        dict[str, object](),
    )
    outcome = observed.get("outcome")
    matched = observed.get("matched")
    killed = observed.get("killed")
    waited_ms = observed.get("waited_ms")
    scan_failure = observed.get("scan_failure")
    if (
        isinstance(outcome, str)
        and outcome in {item.value for item in SweepOutcome}
        and type(matched) is int
        and 0 <= matched <= 65536
        and type(killed) is int
        and 0 <= killed <= matched
        and type(waited_ms) is float
        and isfinite(waited_ms)
        and waited_ms >= 0
    ):
        return {
            "outcome": outcome,
            "matched": matched,
            "killed": killed,
            "waited_ms": waited_ms,
            "scan_failure": (
                scan_failure
                if outcome == SweepOutcome.LISTING_INCOMPLETE.value
                and type(scan_failure) is str
                and scan_failure in {item.value for item in ProcessScanFailure}
                else None
            ),
        }
    return None


@pytest.mark.measurement
@pytest.mark.asyncio
async def test_local_runtime_spawn_cost(tmp_path: Path, kora_capture: Capture) -> None:
    root = state_root(tmp_path)
    ctx = runtime_context(kora_capture)
    descriptors = open_descriptors()
    floors = [floor_seconds() for _ in range(SPAWN_REPEATS)]
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets")
        try:
            await submitted_in(runtime, "warm", ctx)
            single = [
                await timed(submitted_in(runtime, f"single-{index}", ctx))
                for index in range(SPAWN_REPEATS)
            ]
            concurrent = list(
                await asyncio.gather(
                    *(
                        timed(submitted_in(runtime, f"concurrent-{index}", ctx))
                        for index in range(CONCURRENCY)
                    )
                )
            )
            idle = [resident_kib(pid) for pid in runner_pids(root)]
        finally:
            await runtime.close()
            reap_runners(root)
        retention_root = state_root(private_directory(tmp_path, "retention"))
        retention = await open_spawning(
            retention_root, ctx, base / "retention", runner=LINGERING
        )
        trend: list[int] = []
        completed_retention_cycles = 0
        try:
            try:
                for index in range(WARM_UP):
                    await retired_cycle(retention, f"warm-{index}", ctx)
                gc.collect()
                tracemalloc.start()
                snapshot = tracemalloc.take_snapshot()
                first = snapshot
                for index in range(RETENTION_CYCLES):
                    await retired_cycle(retention, f"cycle-{index}", ctx)
                    completed_retention_cycles += 1
                    if (index + 1) % RETENTION_INTERVAL == 0:
                        gc.collect()
                        taken = tracemalloc.take_snapshot()
                        trend.append(component_bytes(taken, snapshot))
                        snapshot = taken
                total = component_bytes(snapshot, first)
                tracemalloc.stop()
                lingering = len(runner_pids(retention_root))
            finally:
                await retention.close()
                reap_runners(retention_root)
        except KoraProtocolError as error:
            if error.code != "runner_stop_unconfirmed":
                raise
            try:
                records = kora_capture.finish()
            except (CaptureTimeout, CaptureOverflow, json.JSONDecodeError) as failure:
                error.add_note("last observed runtime.local.retire: null (unverified)")
                raise error from failure
            last_observed = last_retirement_observation(records)
            error.add_note(
                json.dumps(
                    {
                        "completed_retention_cycles": completed_retention_cycles,
                        "last_observed_runtime.local.retire": last_observed,
                    },
                    sort_keys=True,
                )
            )
            raise

    report = {
        "workload": "local-runtime-spawn",
        "revision": os.environ.get(REVISION_VARIABLE, "local"),
        "platform": f"{sys.platform}-{platform.machine()}",
        "python": platform.python_version(),
        "repeats": SPAWN_REPEATS,
        "concurrency": CONCURRENCY,
        "posix_spawn_python_pass_floor": summary(floors),
        "submit_to_readiness_single": summary(single),
        "submit_to_readiness_concurrent": summary(concurrent),
        "idle_runner_rss_kib": statistics.median(idle) if idle else 0.0,
        "idle_runners_observed": len(idle),
        "retention_cycles": RETENTION_CYCLES,
        "retention_component_bytes": total,
        "retention_interval_cycles": RETENTION_INTERVAL,
        "retention_collected_before_each_snapshot": True,
        "retention_component_bytes_per_interval": trend,
        "retention_runners_left_before_cleanup": lingering,
        "breakdown_of_readiness_latency": "not measured",
        "open_descriptor_delta": open_descriptors() - descriptors,
    }
    published(report)
    assert report["open_descriptor_delta"] == 0


class TimedSource:
    def __init__(self, inner: PeerSource) -> None:
        self._inner = inner
        self.attested: list[float] = []

    def stamp(self) -> int:
        return self._inner.stamp()

    def attest(self, endpoint: socket.socket, accepted: int | None) -> ProcessIdentity:
        started = time.perf_counter()
        try:
            return self._inner.attest(endpoint, accepted)
        finally:
            self.attested.append(time.perf_counter() - started)


class SplitSource:
    def __init__(self, inner: CredentialSource | DarwinSource) -> None:
        self._inner = inner
        self.credentials: list[float] = []
        self.judged: list[float] = []

    def stamp(self) -> int:
        return self._inner.stamp()

    def attest(self, endpoint: socket.socket, accepted: int | None) -> ProcessIdentity:
        started = time.perf_counter()
        credentials = self._inner.credentials(endpoint)
        read = time.perf_counter()
        try:
            return self._inner.judge(credentials, accepted)
        finally:
            self.credentials.append(read - started)
            self.judged.append(time.perf_counter() - read)


class UnattestedSource:
    def __init__(self) -> None:
        self._identity = identity_of(os.getpid())

    def stamp(self) -> int:
        return 0

    def attest(self, endpoint: socket.socket, accepted: int | None) -> ProcessIdentity:
        return self._identity


async def status_round_trips(
    path: str, clock: SystemClock, count: int, concurrency: int
) -> list[float]:
    async def one() -> float:
        started = time.perf_counter()
        channel = await client(path, clock)
        try:
            await asked_status(channel, clock)
        finally:
            channel.close()
        return time.perf_counter() - started

    durations: list[float] = []
    for start in range(0, count, concurrency):
        batch = min(concurrency, count - start)
        durations.extend(await asyncio.gather(*(one() for _ in range(batch))))
    return durations


async def served_round_trips(
    directory: Path, source: PeerSource, capture: Capture, concurrency: int
) -> list[float]:
    clock = SystemClock()
    async with served(directory, clock, source, capture) as (_, path):
        await status_round_trips(path, clock, ATTEST_WARM_UP, 1)
        if isinstance(source, TimedSource):
            source.attested.clear()
        if isinstance(source, SplitSource):
            source.credentials.clear()
            source.judged.clear()
        return await status_round_trips(path, clock, ATTEST_CONNECTIONS, concurrency)


@pytest.mark.measurement
@pytest.mark.asyncio
async def test_local_runtime_attestation_cost(
    tmp_path: Path, kora_capture: Capture
) -> None:
    descriptors = open_descriptors()
    root = state_root(tmp_path)
    ctx = runtime_context(kora_capture)
    kernel = TimedSource(kernel_source(NativeIdentityReader()))
    split = SplitSource(judging_source())
    with short_directory() as base:
        floor = await served_round_trips(
            base / "floor", UnattestedSource(), kora_capture, 1
        )
        sequential = await served_round_trips(base / "kernel", kernel, kora_capture, 1)
        sequential_attestation = list(kernel.attested)
        concurrent = await served_round_trips(
            base / "concurrent", kernel, kora_capture, ATTEST_CONCURRENCY
        )
        concurrent_attestation = list(kernel.attested)
        await served_round_trips(base / "split", split, kora_capture, 1)
        retention: list[int] = []
        clock = SystemClock()
        async with served(base / "retention", clock, kernel, kora_capture) as (
            _,
            path,
        ):
            await status_round_trips(path, clock, ATTEST_WARM_UP, 1)
            gc.collect()
            tracemalloc.start()
            snapshot = tracemalloc.take_snapshot()
            first = snapshot
            for _ in range(RETENTION_CYCLES):
                await status_round_trips(path, clock, RETENTION_INTERVAL, 1)
                gc.collect()
                taken = tracemalloc.take_snapshot()
                retention.append(component_bytes(taken, snapshot))
                snapshot = taken
            retained = component_bytes(snapshot, first)
            tracemalloc.stop()
        runtime = await open_spawning(root, ctx, base / "sockets")
        try:
            handle = await runtime.submit(submitted(runtime, "attempt-1"), ctx)
            await runtime.status(handle, ctx)
            per_call = [
                await timed(runtime.status(handle, ctx))
                for _ in range(ATTEST_CONNECTIONS)
            ]
        finally:
            await runtime.close()
            reap_runners(root)

    report = {
        "workload": "local-runtime-attestation",
        "revision": os.environ.get(REVISION_VARIABLE, "local"),
        "platform": f"{sys.platform}-{platform.machine()}",
        "python": platform.python_version(),
        "connections": ATTEST_CONNECTIONS,
        "warm_up": ATTEST_WARM_UP,
        "concurrency": ATTEST_CONCURRENCY,
        "kernel_source": type(kernel_source(NativeIdentityReader())).__name__,
        "status_round_trip_without_attestation": summary(floor),
        "status_round_trip_sequential": summary(sequential),
        "status_round_trip_concurrent": summary(concurrent),
        "kernel_attestation_sequential": summary(sequential_attestation),
        "kernel_attestation_concurrent": summary(concurrent_attestation),
        "credential_read": summary(split.credentials),
        "identity_reads_and_window": summary(split.judged),
        "worker_status_per_call": summary(per_call),
        "runner_retention_cycles": RETENTION_CYCLES * RETENTION_INTERVAL,
        "runner_retention_component_bytes": retained,
        "runner_retention_bytes_per_interval": retention,
        "open_descriptor_delta": open_descriptors() - descriptors,
    }
    published(report)
    assert report["open_descriptor_delta"] == 0
