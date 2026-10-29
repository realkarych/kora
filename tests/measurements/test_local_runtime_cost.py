import asyncio
import json
import os
import platform
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.protocols.common import ProtocolContext
from kora.runner.registry import Registry, bind_workspace, retire_cell
from tests.measurements.harness import OUTPUT_VARIABLE, REVISION_VARIABLE
from tests.support.local_runtime import (
    OpenedRegistry,
    open_descriptors,
    open_registry,
    runtime_context,
    state_root,
    stopped_holder,
)

__all__ = ["kora_capture"]

REPEATS = 50
WARM_UP = 5
LARGE_REGISTRY = 10000
CONCURRENCY = 10


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


async def bound(registry: OpenedRegistry, attempt: str, ctx: ProtocolContext) -> None:
    await registry.transition(
        attempt, lambda entry: bind_workspace(entry, None, False), ctx
    )


async def retired(registry: OpenedRegistry, attempt: str, ctx: ProtocolContext) -> None:
    key = registry.key(attempt)
    stopped_at = ctx.clock.now()
    await registry.transition(
        attempt, lambda entry: retire_cell(entry, key, "retirement-1", stopped_at), ctx
    )


@pytest.mark.measurement
@pytest.mark.asyncio
async def test_local_runtime_lock_contention(
    tmp_path: Path, kora_capture: Capture
) -> None:
    root = state_root(tmp_path)
    ctx = runtime_context(kora_capture)
    descriptors = open_descriptors()
    opened_at = time.perf_counter()
    runtime = await open_registry(root, ctx)
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
        lock = root / "registry" / f"{runtime.key('stopped').name}.lock"
        await bound(runtime, "stopped", ctx)
        with stopped_holder(lock):
            stopped_started = time.perf_counter()
            refused = await asyncio.gather(
                bound(runtime, "stopped", ctx), return_exceptions=True
            )
            stopped_wait = time.perf_counter() - stopped_started
    finally:
        await runtime.close()

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
        "open_descriptor_delta": open_descriptors() - descriptors,
    }
    output = os.environ.get(OUTPUT_VARIABLE)
    if output is not None:
        Path(output).write_text(json.dumps(report, indent=2, sort_keys=True))
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    assert report["open_descriptor_delta"] == 0
