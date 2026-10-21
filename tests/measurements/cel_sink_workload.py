import asyncio
import json
import os
import threading
import time
import tracemalloc
from dataclasses import asdict

from kora.cel_eval import CEL_COMPONENT
from kora.cel_eval.errors import ErrorContext, EvaluatorUnavailable
from kora.cel_eval.thread import EvaluationThread, Evaluator
from kora.observability import Level, LoggingSettings, configure_process
from tests.cel_eval.test_cel_conversion import expression
from tests.cel_eval.test_cel_evaluation import clock
from tests.cel_eval.thread_scenarios import ScriptedBinding
from tests.measurements.cel_workload import (
    CYCLES,
    SAMPLE_EVERY,
    WARMUP,
    quiesce,
    sample,
)
from tests.measurements.harness import open_descriptors


class CountingReader:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.bytes = 0
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self) -> None:
        while data := os.read(self.descriptor, 65536):
            self.bytes += len(data)

    def finish(self) -> None:
        self.thread.join(5)
        assert not self.thread.is_alive()


def fill(descriptor: int) -> int:
    os.set_blocking(descriptor, False)
    written = 0
    while True:
        try:
            written += os.write(descriptor, b"x" * 4096)
        except BlockingIOError:
            os.set_blocking(descriptor, True)
            return written


def run(sink: str, level: str, exit_running: bool = False) -> str:
    baseline_descriptors = open_descriptors()
    read_end, write_end = os.pipe()
    reader: CountingReader | None = None
    filled_pipe_bytes = 0
    if sink == "draining":
        reader = CountingReader(read_end)
    elif sink == "blocked":
        filled_pipe_bytes = fill(write_end)
    elif sink == "broken":
        os.close(read_end)
    settings = LoggingSettings(level=Level[level], queue_records=16, queue_bytes=65536)
    diagnostics = configure_process(
        CEL_COMPONENT, settings, "sink-measurement", clock, write_end
    )
    fake = ScriptedBinding(block="true" if exit_running else None, fail="false")
    owner = EvaluationThread(fake.binding())
    checked = expression("true")
    failed = expression("false")
    points: list[dict[str, int | None]] = []
    retaining_paths: list[str] = []
    started = time.perf_counter()

    async def workload() -> None:
        tracemalloc.start()
        retained = tracemalloc.take_snapshot()
        for cycle in range(-WARMUP, CYCLES):
            evaluator = Evaluator(clock, owner)
            assert await evaluator.evaluate(checked, {}, {}, ErrorContext("when"))
            await evaluator.close()
            quiesce(owner)
            fake.executed.clear()
            fake.order.clear()
            if cycle >= 0 and (cycle + 1) % SAMPLE_EVERY == 0:
                point: dict[str, int | None] = asdict(sample(cycle + 1, evaluator))
                state = diagnostics.snapshot()
                point.update(
                    queue_records=state.queue_records,
                    queue_bytes=state.queue_bytes,
                    lifecycle_reports=owner.snapshot().diagnostics,
                )
                points.append(point)
                if cycle + 1 == 100:
                    retained = tracemalloc.take_snapshot()
        top = tracemalloc.take_snapshot().compare_to(retained, "lineno")[:10]
        retaining_paths.extend(str(entry) for entry in top)
        tracemalloc.stop()
        evaluator = Evaluator(clock, owner)
        try:
            await evaluator.evaluate(failed, {}, {}, ErrorContext("when"))
        except EvaluatorUnavailable:
            pass
        await evaluator.close()
        owner.worker.join(5)
        assert not owner.worker.is_alive()
        assert owner.snapshot().outstanding == 0
        assert owner.snapshot().handles == 0
        assert owner.snapshot().bytes == 0

    if exit_running:
        from kora.cel_eval.convert import BoundValues
        from kora.cel_eval.evaluate import Prepared, ResultType
        from kora.cel_eval.thread import EvaluationWork

        loop = asyncio.new_event_loop()
        identifier, _ = owner.open(loop)
        request = Prepared(
            checked, BoundValues({}, 0, 0), ErrorContext("when"), ResultType.BOOL
        )
        owner.submit(identifier, EvaluationWork((request,)), clock, 100)
        assert fake.started.wait(5)
        loop.close()
    else:
        asyncio.run(workload())
    snapshot = diagnostics.snapshot()
    shutdown_started = time.perf_counter()
    drained = diagnostics.shutdown(0.01 if sink == "blocked" else 1.0)
    shutdown_seconds = time.perf_counter() - shutdown_started
    if sink != "blocked":
        os.close(write_end)
    if reader is not None:
        reader.finish()
        os.close(read_end)
    assert diagnostics.snapshot().queue_bytes == 0
    assert diagnostics.snapshot().queue_records == 0
    assert open_descriptors() == baseline_descriptors + (2 if sink == "blocked" else 0)
    return json.dumps(
        {
            "stats": {
                "work_seconds": time.perf_counter() - started,
                "shutdown_seconds": shutdown_seconds,
                "dropped": snapshot.dropped_total(),
                "sink_failed": snapshot.sink_failed,
                "drained": drained,
                "running": exit_running,
                "writer_alive_at_exit": diagnostics.writer_is_alive(),
                "filled_pipe_bytes": filled_pipe_bytes,
            },
            "points": points,
            "retaining_paths": retaining_paths,
        }
    )
