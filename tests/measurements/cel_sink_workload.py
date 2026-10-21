import asyncio
import json
import os
import time

from kora.cel_eval import CEL_COMPONENT
from kora.cel_eval.errors import ErrorContext, EvaluatorUnavailable
from kora.cel_eval.thread import EvaluationThread, Evaluator
from kora.observability import Level, LoggingSettings, configure_process
from tests.cel_eval.test_cel_conversion import expression
from tests.cel_eval.test_cel_evaluation import clock
from tests.cel_eval.thread_scenarios import ScriptedBinding
from tests.measurements.cel_workload import quiesce
from tests.observability.conftest import PipeReader


def fill(descriptor: int) -> None:
    os.set_blocking(descriptor, False)
    while True:
        try:
            os.write(descriptor, b"x" * 4096)
        except BlockingIOError:
            return


def run(sink: str, level: str, exit_running: bool = False) -> str:
    read_end, write_end = os.pipe()
    reader: PipeReader | None = None
    if sink == "draining":
        reader = PipeReader(read_end)
        reader.start()
    elif sink == "blocked":
        fill(write_end)
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
    started = time.perf_counter()

    async def workload() -> None:
        for _ in range(200):
            evaluator = Evaluator(clock, owner)
            assert await evaluator.evaluate(checked, {}, {}, ErrorContext("when"))
            await evaluator.close()
            quiesce(owner)
        evaluator = Evaluator(clock, owner)
        try:
            await evaluator.evaluate(failed, {}, {}, ErrorContext("when"))
        except EvaluatorUnavailable:
            pass
        await evaluator.close()
        owner.worker.join(5)
        assert not owner.worker.is_alive()
        assert owner.snapshot().outstanding == 0

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
    drained = diagnostics.shutdown(0.01 if sink == "blocked" else 1.0)
    os.close(write_end)
    if reader is not None:
        reader.wait_closed()
        reader.join()
        os.close(read_end)
    elif sink == "blocked":
        os.close(read_end)
    return json.dumps(
        {
            "seconds": time.perf_counter() - started,
            "dropped": snapshot.dropped_total(),
            "sink_failed": snapshot.sink_failed,
            "drained": drained,
            "running": exit_running,
        }
    )
