import json

import pytest

from tests.cel_eval.children import ROOT, full_path, run_child

SETUP: str = """
import asyncio
import json
import threading
from dataclasses import asdict
import structlog.contextvars
from kora.observability import LoggingSettings, configure_process, Level
from kora.observability.settings import RenderFormat
from kora.cel_eval import CEL_COMPONENT
from kora.cel_eval.thread import Evaluator
from kora.cel_eval.errors import ErrorContext, EvaluationError
from kora.cel_eval.evaluate import ResultType
from kora.cel.types import DYN
from tests.cel_eval.test_cel_conversion import expression
from tests.cel_eval.test_cel_evaluation import clock
"""
LIFECYCLE: str = """
async def main():
    structlog.contextvars.bind_contextvars(request_id='creating-request-marker')
    first = Evaluator(clock)
    second = Evaluator(clock)
    sources = [
        'inputs.privateIdentifier == "privateLiteral"',
        'inputs.privateKey + 1 == 0',
        '"privateLiteral".matches("privatePattern") || inputs.privateMissing == 0',
    ]
    records = []
    for source in sources:
        result = await first.evaluate(expression(source),
                                      {'inputs': {'privateKey': 'privatePayload'}},
                                      {'inputs': DYN}, ErrorContext('nodes.safe.when'))
        assert isinstance(result, EvaluationError)
        records.append(asdict(result))
    print(json.dumps(records, default=str))
    await first.close()
    assert await second.evaluate(expression('true'), {}, {}, ErrorContext('when'))
    await second.close()
    with second.owner.condition:
        assert second.owner.condition.wait_for(
            lambda: second.snapshot().diagnostics == 0, 5)
asyncio.run(main())
assert diagnostics.shutdown(5)
"""


@pytest.mark.parametrize("render_format", ["json", "text"])
def test_cel_persisted_errors_exclude_values(render_format: str) -> None:
    configured = (
        "diagnostics = configure_process(CEL_COMPONENT, "
        "LoggingSettings(level=Level.DEBUG, "
        f"render_format=RenderFormat({render_format!r})), "
        "'evaluator-test', clock)\n"
    )

    result = run_child(SETUP + configured + LIFECYCLE, full_path(ROOT))

    assert result.code == 0, result.stderr
    errors = json.loads(result.stdout)
    error = errors[0]
    assert error["kind"] == "missing_field"
    assert error["field"] == "nodes.safe.when"
    assert error["fault"] is None
    for marker in (
        "privateIdentifier",
        "privateLiteral",
        "privatePattern",
        "privateKey",
        "privatePayload",
        "privateMissing",
        "creating-request-marker",
    ):
        assert marker not in result.stdout + result.stderr
    assert "cel.evaluator.started" in result.stderr
    assert "cel.evaluator.stopped" in result.stderr


def test_cel_evaluator_lifecycle_diagnostics() -> None:
    configured = (
        "diagnostics = configure_process(CEL_COMPONENT, LoggingSettings(), "
        "'evaluator-test', clock)\n"
    )

    result = run_child(SETUP + configured + LIFECYCLE, full_path(ROOT))

    assert result.code == 0, result.stderr
    records: list[dict[str, object]] = [
        json.loads(line) for line in result.stderr.splitlines()
    ]
    starts = [r for r in records if r["event"] == "cel.evaluator.started"]
    stops = [r for r in records if r["event"] == "cel.evaluator.stopped"]
    assert len(starts) == 1
    assert len(stops) == 2
    assert all(r["level"] == "INFO" and r["rejected_jobs"] == 0 for r in stops)
    assert all("request_id" not in r for r in starts + stops)


def test_cel_evaluator_diagnostic_failure_does_not_strand_callers() -> None:
    code = (
        SETUP
        + """
from kora.cel_eval.thread import EvaluationThread
from tests.cel_eval.thread_scenarios import ScriptedBinding
from kora.cel_eval.errors import EvaluatorUnavailable, UnavailableReason

def diagnostic_clock():
    if threading.current_thread().name == 'kora-cel-evaluator':
        raise ArithmeticError('private-diagnostic-clock')
    return clock()

diagnostics = configure_process(CEL_COMPONENT, LoggingSettings(),
                                'evaluator-test', diagnostic_clock)
owner = EvaluationThread(ScriptedBinding().binding())
owner.worker.join(5)
assert not owner.worker.is_alive()
assert owner.snapshot().failed
assert owner.snapshot().outstanding == 0
assert owner.snapshot().handles == 0
async def main():
    try:
        Evaluator(clock, owner)
    except EvaluatorUnavailable as error:
        assert error.reason is UnavailableReason.FAILED
        assert error.__cause__ is None and error.__context__ is None
    else:
        raise AssertionError('failed evaluator accepted a handle')
asyncio.run(main())
assert diagnostics.shutdown(5)
"""
    )

    result = run_child(code, full_path(ROOT))

    assert result.code == 0, result.stderr
    assert "private-diagnostic-clock" not in result.stdout + result.stderr


def test_cel_lifecycle_rejections_timeout_and_terminal_failure() -> None:
    code = (
        SETUP
        + """
from kora.cel_eval.thread import EvaluationThread, Settings
from kora.cel_eval.errors import EvaluatorUnavailable
from tests.cel_eval.thread_scenarios import ScriptedBinding, reached, turn
from tests.measurements.cel_workload import quiesce

diagnostics = configure_process(
    CEL_COMPONENT, LoggingSettings(), 'evaluator-test', clock)
fake = ScriptedBinding(block='true', fail='(99 == 99)')
owner = EvaluationThread(fake.binding(), Settings(close_timeout=0.001))
async def main():
    first = Evaluator(clock, owner)
    running = asyncio.create_task(
        first.evaluate(expression('true'), {}, {}, ErrorContext('when')))
    await reached(fake.started)
    queued = asyncio.create_task(
        first.evaluate(expression('false'), {}, {}, ErrorContext('when')))
    await turn()
    for _ in range(2):
        try:
            await first.close()
        except EvaluatorUnavailable:
            pass
    fake.release.set()
    assert await running is True
    try:
        await queued
    except EvaluatorUnavailable:
        pass
    await first.close()
    quiesce(owner)
    final = Evaluator(clock, owner)
    try:
        await final.evaluate(expression('99 == 99'), {}, {}, ErrorContext('when'))
    except EvaluatorUnavailable:
        pass
    await final.close()
    owner.worker.join(5)
    assert not owner.worker.is_alive()
asyncio.run(main())
assert diagnostics.shutdown(5)
"""
    )

    result = run_child(code, full_path(ROOT))

    assert result.code == 0, result.stderr
    records: list[dict[str, object]] = [
        json.loads(line) for line in result.stderr.splitlines()
    ]
    timeouts = [r for r in records if r["event"] == "cel.evaluator.close_timed_out"]
    failures = [r for r in records if r["event"] == "cel.evaluator.failed"]
    stops = [r for r in records if r["event"] == "cel.evaluator.stopped"]
    assert len(timeouts) == 1
    assert timeouts[0]["level"] == "WARNING"
    assert len(failures) == 1
    assert failures[0]["level"] == "ERROR"
    assert failures[0]["fault"] == "unmapped"
    assert len(stops) == 1
    assert stops[0]["rejected_jobs"] == 1


def test_cel_diagnostic_failure_releases_existing_waiters() -> None:
    code = (
        SETUP
        + """
from kora.cel_eval.thread import EvaluationThread
from kora.cel_eval.errors import EvaluatorUnavailable, UnavailableReason
from tests.cel_eval.thread_scenarios import ScriptedBinding, reached, turn
from tests.measurements.cel_workload import quiesce

failure_enabled = threading.Event()
def diagnostic_clock():
    worker = threading.current_thread().name == 'kora-cel-evaluator'
    if failure_enabled.is_set() and worker:
        raise ArithmeticError('private-diagnostic-cause')
    return clock()

diagnostics = configure_process(
    CEL_COMPONENT, LoggingSettings(), 'evaluator-test', diagnostic_clock)
fake = ScriptedBinding(block='true')
owner = EvaluationThread(fake.binding())
async def main():
    first = Evaluator(clock, owner)
    idle = Evaluator(clock, owner)
    waiting = Evaluator(clock, owner)
    running = asyncio.create_task(
        first.evaluate(expression('true'), {}, {}, ErrorContext('when')))
    await reached(fake.started)
    queued = asyncio.create_task(
        waiting.evaluate(expression('false'), {}, {}, ErrorContext('when')))
    await turn()
    await idle.close()
    closing = asyncio.create_task(first.close())
    await turn()
    failure_enabled.set()
    fake.release.set()
    outcomes = await asyncio.gather(running, queued, closing, return_exceptions=True)
    assert isinstance(outcomes[1], EvaluatorUnavailable)
    assert outcomes[1].reason is UnavailableReason.FAILED
    assert outcomes[2] is None
    await waiting.close()
    owner.worker.join(5)
    assert not owner.worker.is_alive()
    assert owner.snapshot().handles == 0
    assert owner.snapshot().outstanding == 0
    assert owner.snapshot().bytes == 0
asyncio.run(main())
assert diagnostics.shutdown(5)
"""
    )

    result = run_child(code, full_path(ROOT))

    assert result.code == 0, result.stderr
    assert "private-diagnostic-cause" not in result.stdout + result.stderr
