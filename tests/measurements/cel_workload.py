import asyncio
import gc
import json
import os
import threading
import time
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path

from kora.cel.types import DYN
from kora.cel_eval.errors import ErrorContext, EvaluatorUnavailable
from kora.cel_eval.thread import EvaluationInput, EvaluationThread, Evaluator, Settings
from tests.cel_eval.test_cel_conversion import expression
from tests.cel_eval.test_cel_evaluation import clock
from tests.cel_eval.thread_scenarios import ScriptedBinding, reached, turn
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


def sample(cycle: int, evaluator: Evaluator) -> Point:
    gc.collect()
    heap, peak = tracemalloc.get_traced_memory()
    state = evaluator.snapshot()
    tasks_path = Path("/proc/self/task")
    return Point(
        cycle=cycle,
        heap=heap,
        peak=peak,
        rss=current_rss_bytes(),
        descriptors=open_descriptors(),
        threads=threading.active_count(),
        os_threads=len(os.listdir(tasks_path)) if tasks_path.is_dir() else None,
        tasks=len(asyncio.all_tasks()),
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
    sources = [
        "true",
        "false",
        "inputs.missing == 1",
        "inputs.n.x == 1",
        "inputs.items[9] == 1",
        "1 / 0 == 0",
        "9223372036854775807 + 1 == 0",
        "inputs.items.size() == 1000",
        "inputs.s.size() == 16384",
    ]
    values = {"inputs": {"n": None, "items": [0] * 1000, "s": "a" * 16384}}
    requests = [
        EvaluationInput(expression(s), values, {"inputs": DYN}, ErrorContext("when"))
        for s in sources
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
        await evaluator.evaluate_batch(requests)
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
    fake = ScriptedBinding(block="true", fail="(99999 == 99999)")
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


def run(mode: str) -> str:
    if mode == "native":
        result = asyncio.run(native())
    elif mode == "lifetime":
        result = asyncio.run(lifetime())
    else:
        result = asyncio.run(hops(mode == "busy"))
    return json.dumps(asdict(result))
