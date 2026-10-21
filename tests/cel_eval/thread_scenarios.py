import asyncio
import threading

import pytest

from kora.cel_eval.binding import Binding, NativeProgram
from kora.cel_eval.errors import ErrorContext, EvaluatorUnavailable, UnavailableReason
from tests.cel_eval.test_cel_conversion import expression
from tests.cel_eval.test_cel_evaluation import clock


class Panic(BaseException):
    pass


class UnmappedFault(Exception):
    pass


class ScriptedProgram:
    def __init__(self, source: str, controller: "ScriptedBinding") -> None:
        self.source = source
        self.controller = controller

    def execute(self, context: dict[str, object]) -> object:
        self.controller.executed.append(self.source)
        self.controller.mark("execute", self.source)
        if self.source == self.controller.block:
            self.controller.started.set()
            if not self.controller.release.wait(5):
                raise TimeoutError("scripted execution was not released")
        if self.source == self.controller.fail:
            raise UnmappedFault("private-expression-and-payload")
        return self.source != "false"


class ScriptedBinding:
    def __init__(self, block: str | None = None, fail: str | None = None) -> None:
        self.block = block
        self.fail = fail
        self.started = threading.Event()
        self.release = threading.Event()
        self.executed: list[str] = []
        self.order: list[tuple[str, str]] = []
        self.condition = threading.Condition()

    def mark(self, operation: str, source: str) -> None:
        with self.condition:
            self.order.append((operation, source))
            self.condition.notify_all()

    def compile(self, expression: str) -> NativeProgram:
        self.mark("compile", expression)
        return ScriptedProgram(expression, self)

    def binding(self) -> Binding:
        return Binding("fake", Panic, self.compile)


async def reached(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 5)


async def turn() -> None:
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()
    loop.call_soon(done.set_result, None)
    await done


async def priority_cancel_close() -> None:
    from kora.cel_eval.thread import EvaluationThread, Evaluator

    fake = ScriptedBinding(block="true")
    owner = EvaluationThread(fake.binding())
    first = Evaluator(clock, owner=owner)
    second = Evaluator(clock, owner=owner)
    running = asyncio.create_task(
        first.evaluate(expression("true"), {}, {}, ErrorContext("when"))
    )
    await reached(fake.started)
    compile_job = asyncio.create_task(second.validate(expression("2 == 2")))
    cancelled = asyncio.create_task(
        first.evaluate(expression("false"), {}, {}, ErrorContext("when"))
    )
    urgent = asyncio.create_task(
        second.evaluate(expression("1 == 1"), {}, {}, ErrorContext("when"))
    )
    await turn()

    cancelled.cancel()
    closing = asyncio.create_task(first.close())
    await turn()
    fake.release.set()
    assert await running is True
    assert await urgent is True
    assert (await compile_job).valid
    await closing
    await second.close()
    try:
        await cancelled
    except asyncio.CancelledError:
        pass

    assert "false" not in fake.executed
    assert fake.order.index(("execute", "(1 == 1)")) < fake.order.index(
        ("compile", "(2 == 2)")
    )
    assert owner.snapshot().outstanding == 0
    assert owner.snapshot().handles == 0


async def fatal_failure() -> None:
    from kora.cel_eval.thread import EvaluationThread, Evaluator

    fake = ScriptedBinding(block="true", fail="true")
    owner = EvaluationThread(fake.binding())
    evaluator = Evaluator(clock, owner=owner)
    first = asyncio.create_task(
        evaluator.evaluate(expression("true"), {}, {}, ErrorContext("when"))
    )
    await reached(fake.started)
    queued = asyncio.create_task(
        evaluator.evaluate(expression("false"), {}, {}, ErrorContext("when"))
    )
    await turn()

    fake.release.set()
    outcomes = await asyncio.gather(first, queued, return_exceptions=True)
    await evaluator.close()

    assert all(
        isinstance(result, EvaluatorUnavailable)
        and result.reason is UnavailableReason.FAILED
        for result in outcomes
    )
    assert owner.snapshot().failed
    assert owner.snapshot().outstanding == 0
    assert fake.executed == ["true"]
    with pytest.raises(EvaluatorUnavailable) as caught:
        await evaluator.evaluate(expression("true"), {}, {}, ErrorContext("when"))
    assert caught.value.reason is UnavailableReason.FAILED


def closed_loop() -> None:
    from kora.cel_eval.thread import EvaluationThread, ValidationWork

    fake = ScriptedBinding()
    owner = EvaluationThread(fake.binding())
    loop = asyncio.new_event_loop()
    identifier, handle = owner.open(loop)
    pending = owner.submit(identifier, ValidationWork(expression("true")), clock, 100)
    with owner.condition:
        assert owner.condition.wait_for(lambda: owner.snapshot().deliveries == 1, 5)

    loop.close()
    with owner.condition:
        assert owner.condition.wait_for(lambda: owner.snapshot().handles == 0, 5)

    assert not pending.done()
    assert handle.closed
    assert owner.snapshot().outstanding == 0
    assert owner.snapshot().bytes == 0
    assert not owner.snapshot().failed


def stopped_loop() -> None:
    from kora.cel_eval.thread import EvaluationThread, Settings, ValidationWork

    owner = EvaluationThread(ScriptedBinding().binding(), Settings(compile_jobs=1))
    loop = asyncio.new_event_loop()
    identifier, handle = owner.open(loop)
    pending = owner.submit(identifier, ValidationWork(expression("true")), clock, 100)
    with owner.condition:
        assert owner.condition.wait_for(lambda: owner.snapshot().deliveries == 1, 5)

    with pytest.raises(EvaluatorUnavailable) as caught:
        owner.submit(identifier, ValidationWork(expression("false")), clock, 100)
    result = loop.run_until_complete(pending)
    owner.close(identifier, handle)
    loop.run_until_complete(handle.finished)
    loop.close()

    assert caught.value.reason is UnavailableReason.BUSY
    from kora.cel_eval.evaluate import Compilation

    assert isinstance(result, Compilation)
    assert result.valid
    assert owner.snapshot().bytes == 0
    assert owner.snapshot().outstanding == 0


async def close_timeout() -> None:
    from kora.cel_eval.thread import EvaluationThread, Evaluator, Settings

    fake = ScriptedBinding(block="true")
    owner = EvaluationThread(fake.binding(), Settings(close_timeout=0.001))
    evaluator = Evaluator(clock, owner=owner)
    running = asyncio.create_task(
        evaluator.evaluate(expression("true"), {}, {}, ErrorContext("when"))
    )
    await reached(fake.started)

    with pytest.raises(EvaluatorUnavailable) as caught:
        await evaluator.close()
    assert caught.value.reason is UnavailableReason.CLOSE_TIMED_OUT
    fake.release.set()
    assert await running is True
    await evaluator.close()

    assert owner.snapshot().outstanding == 0
    assert owner.snapshot().handles == 0


async def batch_limits() -> None:
    from kora.cel_eval.thread import (
        EvaluationInput,
        EvaluationThread,
        Evaluator,
        Settings,
    )

    fake = ScriptedBinding()
    owner = EvaluationThread(fake.binding(), Settings(batch_size=2))
    evaluator = Evaluator(clock, owner=owner)
    request = EvaluationInput(expression("true"), {}, {}, ErrorContext("when"))

    accepted = await evaluator.evaluate_batch([request, request])
    with pytest.raises(EvaluatorUnavailable) as caught:
        await evaluator.evaluate_batch([request, request, request])
    assert caught.value.reason is UnavailableReason.BUSY
    await evaluator.close()

    assert accepted == (True, True)
    assert len(fake.executed) == 2


def run(name: str) -> None:
    synchronous = {
        "closed_loop": closed_loop,
        "stopped_loop": stopped_loop,
        "fatal_closed_loop": fatal_closed_loop,
    }
    if name in synchronous:
        synchronous[name]()
        return
    scenarios = {
        "priority_cancel_close": priority_cancel_close,
        "fatal_failure": fatal_failure,
        "close_timeout": close_timeout,
        "batch_limits": batch_limits,
    }
    asyncio.run(scenarios[name]())


def fatal_closed_loop() -> None:
    from kora.cel_eval.convert import BoundValues
    from kora.cel_eval.evaluate import Prepared, ResultType
    from kora.cel_eval.thread import EvaluationThread, EvaluationWork

    owner = EvaluationThread(ScriptedBinding(fail="true").binding())
    loop = asyncio.new_event_loop()
    identifier, _ = owner.open(loop)
    request = Prepared(
        expression("true"), BoundValues({}, 0, 0), ErrorContext("when"), ResultType.BOOL
    )
    owner.submit(identifier, EvaluationWork((request,)), clock, 100)

    owner.worker.join(5)
    loop.close()

    assert not owner.worker.is_alive()
    assert owner.snapshot().failed
    assert owner.snapshot().outstanding == 0
    assert owner.snapshot().handles == 0
    assert owner.snapshot().bytes == 0
