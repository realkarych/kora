import asyncio
import gc
import json
import os
import resource
import sys
import threading
import time
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path

from kora.cel.types import DYN
from kora.cel_eval.errors import (
    ErrorContext,
    ErrorKind,
    EvaluationError,
    EvaluatorUnavailable,
)
from kora.cel_eval.thread import EvaluationInput, EvaluationThread, Evaluator, Settings
from tests.cel_eval.test_cel_conversion import expression
from tests.cel_eval.test_cel_evaluation import clock
from tests.cel_eval.thread_scenarios import Panic, ScriptedBinding, reached, turn
from tests.measurements.harness import current_rss_bytes, open_descriptors

WARMUP: int = 20
CYCLES: int = 200
SAMPLE_EVERY: int = 10


@dataclass(frozen=True)
class Point:
    cycle: int
    heap: int
    peak: int
    rss: int | None
    peak_rss: int
    descriptors: int
    threads: int
    os_threads: int | None
    tasks: int
    handles: int
    outstanding: int
    payload_bytes: int
    cache_entries: int
    cache_characters: int


@dataclass(frozen=True)
class Measurements:
    mode: str
    seconds: float
    points: list[Point]
    timings: list[float]
    retaining_paths: list[str]
    stack_restored: bool
    persistent_workers: int
    terminal_workers: int | None = None


def workers() -> int:
    return sum(t.name == "kora-cel-evaluator" for t in threading.enumerate())


def sample(cycle: int, evaluator: Evaluator, tasks: int | None = None) -> Point:
    gc.collect()
    heap, peak = tracemalloc.get_traced_memory()
    state = evaluator.snapshot()
    tasks_path = Path("/proc/self/task")
    return Point(
        cycle=cycle,
        heap=heap,
        peak=peak,
        rss=current_rss_bytes(),
        peak_rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1024 if sys.platform == "linux" else 1),
        descriptors=open_descriptors(),
        threads=threading.active_count(),
        os_threads=len(os.listdir(tasks_path)) if tasks_path.is_dir() else None,
        tasks=len(asyncio.all_tasks()) if tasks is None else tasks,
        handles=state.handles,
        outstanding=state.outstanding,
        payload_bytes=state.bytes,
        cache_entries=state.cache_entries,
        cache_characters=state.cache_characters,
    )


def quiesce(owner: EvaluationThread) -> None:
    with owner.condition:
        assert owner.condition.wait_for(lambda: owner.snapshot().diagnostics == 0, 5)


async def native() -> Measurements:
    cases: list[tuple[str, dict[str, object]]] = [
        ("true", {}),
        ("false", {}),
        ("inputs.missing == 1", {"inputs": {}}),
        ("inputs.n.x == 1", {"inputs": {"n": None}}),
        ("inputs.items[1000] == 1", {"inputs": {"items": [0] * 1000}}),
        ("1 / 0 == 0", {}),
        ("9223372036854775807 + 1 == 0", {}),
        ("inputs.items.size() == 1000", {"inputs": {"items": [0] * 1000}}),
        ("inputs.s.size() == 16384", {"inputs": {"s": "a" * 16384}}),
    ]
    requests = [
        EvaluationInput(expression(s), values, {"inputs": DYN}, ErrorContext("when"))
        for s, values in cases
    ]
    turnover = [expression(f"{i} == {i}") for i in range(CYCLES + WARMUP)]
    previous_stack = threading.stack_size()
    initial = Evaluator(clock)
    await initial.close()
    points: list[Point] = []
    tracemalloc.start()
    retained = tracemalloc.take_snapshot()
    started = time.perf_counter()
    for cycle in range(-WARMUP, CYCLES):
        evaluator = Evaluator(clock)
        outcomes = await evaluator.evaluate_batch(requests)
        expected: tuple[bool | ErrorKind, ...] = (
            True,
            False,
            ErrorKind.MISSING_FIELD,
            ErrorKind.NO_OVERLOAD,
            ErrorKind.INDEX_OUT_OF_RANGE,
            ErrorKind.DIVISION_BY_ZERO,
            ErrorKind.OVERFLOW,
            True,
            True,
        )
        assert (
            tuple(
                item.kind if isinstance(item, EvaluationError) else item
                for item in outcomes
            )
            == expected
        )
        assert await evaluator.evaluate(
            turnover[cycle + WARMUP], {}, {}, ErrorContext("turnover")
        )
        before = evaluator.snapshot()
        assert (await evaluator.validate(turnover[cycle + WARMUP])).valid
        assert evaluator.snapshot().cache_entries == before.cache_entries
        await evaluator.close()
        quiesce(evaluator.owner)
        assert evaluator.snapshot().outstanding == 0
        assert evaluator.snapshot().handles == 0
        if cycle >= 0 and (cycle + 1) % SAMPLE_EVERY == 0:
            points.append(sample(cycle + 1, evaluator))
            if cycle + 1 == 100:
                retained = tracemalloc.take_snapshot()
    top = tracemalloc.take_snapshot().compare_to(retained, "lineno")[:10]
    tracemalloc.stop()
    return Measurements(
        mode="native",
        seconds=time.perf_counter() - started,
        points=points,
        timings=[],
        retaining_paths=[str(entry) for entry in top],
        stack_restored=threading.stack_size() == previous_stack,
        persistent_workers=workers(),
    )


async def lifetime() -> Measurements:
    failures: tuple[tuple[type[BaseException], ErrorKind], ...] = (
        (KeyError, ErrorKind.MISSING_FIELD),
        (TypeError, ErrorKind.NO_OVERLOAD),
        (IndexError, ErrorKind.INDEX_OUT_OF_RANGE),
        (ZeroDivisionError, ErrorKind.DIVISION_BY_ZERO),
        (OverflowError, ErrorKind.OVERFLOW),
        (ValueError, ErrorKind.INTERNAL_FAULT),
        (RuntimeError, ErrorKind.INTERNAL_FAULT),
        (Panic, ErrorKind.INTERNAL_FAULT),
    )
    failure_requests = [
        EvaluationInput(expression(f"{i + 10000} == 0"), {}, {}, ErrorContext("when"))
        for i in range(len(failures))
    ]
    failure_map = {
        request.checked.rendering: failure[0]
        for request, failure in zip(failure_requests, failures, strict=True)
    }
    fake = ScriptedBinding(block="true", fail="(99999 == 99999)", errors=failure_map)
    previous_stack = threading.stack_size()
    owner = EvaluationThread(
        fake.binding(), Settings(cache_entries=8, cache_characters=64)
    )
    checked = expression("true")
    cancelled_checked = expression("false")
    fatal = expression("99999 == 99999")
    points: list[Point] = []
    tracemalloc.start()
    retained = tracemalloc.take_snapshot()
    started = time.perf_counter()
    for cycle in range(-WARMUP, CYCLES):
        fake.started.clear()
        fake.release.clear()
        evaluator = Evaluator(clock, owner)
        other = Evaluator(clock, owner)
        running = asyncio.create_task(
            evaluator.evaluate(checked, {}, {}, ErrorContext("when"))
        )
        await reached(fake.started)
        queued = asyncio.create_task(
            evaluator.evaluate(cancelled_checked, {}, {}, ErrorContext("when"))
        )
        await turn()
        queued.cancel()
        if cycle % 2:
            running.cancel()
        closing = asyncio.create_task(evaluator.close())
        await turn()
        fake.release.set()
        await asyncio.gather(running, queued, return_exceptions=True)
        await closing
        assert await other.evaluate(
            expression(f"{cycle + WARMUP} == {cycle + WARMUP}"),
            {},
            {},
            ErrorContext("other"),
        )
        mapped = await other.evaluate_batch(failure_requests)
        assert tuple(
            item.kind if isinstance(item, EvaluationError) else item for item in mapped
        ) == tuple(kind for _, kind in failures)
        await other.close()
        quiesce(owner)
        assert "false" not in fake.executed
        fake.executed.clear()
        fake.order.clear()
        if cycle >= 0 and (cycle + 1) % SAMPLE_EVERY == 0:
            points.append(sample(cycle + 1, evaluator))
            if cycle + 1 == 100:
                retained = tracemalloc.take_snapshot()
    top = tracemalloc.take_snapshot().compare_to(retained, "lineno")[:10]
    persistent = workers()
    final = Evaluator(clock, owner)
    try:
        await final.evaluate(fatal, {}, {}, ErrorContext("when"))
    except EvaluatorUnavailable:
        pass
    await final.close()
    owner.worker.join(5)
    assert owner.snapshot().failed
    assert not owner.worker.is_alive()
    assert owner.snapshot().outstanding == 0
    assert owner.snapshot().cache_entries == 0
    tracemalloc.stop()
    return Measurements(
        mode="lifetime",
        seconds=time.perf_counter() - started,
        points=points,
        timings=[],
        retaining_paths=[str(entry) for entry in top],
        stack_restored=threading.stack_size() == previous_stack,
        persistent_workers=persistent,
        terminal_workers=workers(),
    )


async def hops(busy: bool) -> Measurements:
    evaluator = Evaluator(clock)
    checked = expression("true")
    request = EvaluationInput(checked, {}, {}, ErrorContext("when"))
    timings: list[float] = []
    started = time.perf_counter()
    for cycle in range(WARMUP + CYCLES):
        before = time.perf_counter()
        if busy:
            results = await asyncio.gather(
                *(evaluator.evaluate_batch([request] * 16) for _ in range(8))
            )
            assert all(all(batch) for batch in results)
        else:
            assert await evaluator.evaluate(checked, {}, {}, ErrorContext("when"))
        if cycle >= WARMUP:
            timings.append(time.perf_counter() - before)
    await evaluator.close()
    return Measurements(
        mode="busy" if busy else "idle",
        seconds=time.perf_counter() - started,
        points=[],
        timings=timings,
        retaining_paths=[],
        stack_restored=True,
        persistent_workers=workers(),
    )


def loops() -> Measurements:
    from kora.cel_eval.binding import load_binding
    from kora.cel_eval.evaluate import Compilation
    from kora.cel_eval.thread import ValidationWork

    previous_stack = threading.stack_size()
    owner = EvaluationThread(load_binding())
    checked = expression("true")
    points: list[Point] = []
    tracemalloc.start()
    retained = tracemalloc.take_snapshot()
    started = time.perf_counter()
    for cycle in range(-WARMUP, CYCLES):
        pending_tasks = 0
        for outcome in ("cancel", "close", "resume"):
            loop = asyncio.new_event_loop()

            async def create() -> Evaluator:
                return Evaluator(clock, owner)

            evaluator = loop.run_until_complete(create())
            pending = owner.submit(
                evaluator.identifier, ValidationWork(checked), clock, 1024
            )
            with owner.condition:
                assert owner.condition.wait_for(
                    lambda: owner.snapshot().deliveries == 1, 5
                )
            assert owner.snapshot().outstanding == 1
            if outcome == "close":
                pending_tasks = len(asyncio.all_tasks(loop))
                assert pending_tasks == 0
                loop.close()
                with owner.condition:
                    assert owner.condition.wait_for(
                        lambda: owner.snapshot().handles == 0, 5
                    )
            else:
                if outcome == "cancel":
                    pending.cancel()
                    loop.run_until_complete(
                        asyncio.gather(pending, return_exceptions=True)
                    )
                else:
                    result = loop.run_until_complete(pending)
                    assert isinstance(result, Compilation)
                    assert result.valid
                loop.run_until_complete(evaluator.close())
                pending_tasks = len(asyncio.all_tasks(loop))
                assert pending_tasks == 0
                loop.close()
            quiesce(owner)
            assert owner.snapshot().handles == 0
            assert owner.snapshot().outstanding == 0
        if cycle >= 0 and (cycle + 1) % SAMPLE_EVERY == 0:
            points.append(sample(cycle + 1, evaluator, tasks=pending_tasks))
            if cycle + 1 == 100:
                retained = tracemalloc.take_snapshot()
    top = tracemalloc.take_snapshot().compare_to(retained, "lineno")[:10]
    tracemalloc.stop()
    return Measurements(
        mode="loops",
        seconds=time.perf_counter() - started,
        points=points,
        timings=[],
        retaining_paths=[str(entry) for entry in top],
        stack_restored=threading.stack_size() == previous_stack,
        persistent_workers=workers(),
    )


def run(mode: str) -> str:
    if mode == "loops":
        result = loops()
    elif mode == "native":
        result = asyncio.run(native())
    elif mode == "lifetime":
        result = asyncio.run(lifetime())
    else:
        result = asyncio.run(hops(mode == "busy"))
    return json.dumps(asdict(result))
