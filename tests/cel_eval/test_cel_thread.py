import asyncio

import pytest

from kora.cel_eval.errors import ErrorContext, EvaluatorUnavailable, UnavailableReason
from tests.cel_eval.children import ROOT, full_path, run_child
from tests.cel_eval.test_cel_conversion import expression
from tests.cel_eval.test_cel_evaluation import clock


@pytest.mark.asyncio
async def test_cel_evaluation_thread_is_shared_across_handles() -> None:
    from kora.cel_eval.thread import Evaluator

    first = Evaluator(clock)
    second = Evaluator(clock)
    before = first.snapshot()

    results = await asyncio.gather(
        first.evaluate(expression("true"), {}, {}, ErrorContext("when")),
        second.evaluate(expression("false"), {}, {}, ErrorContext("when")),
    )
    await first.close()
    remaining = await second.evaluate(expression("true"), {}, {}, ErrorContext("when"))
    await second.close()

    assert results == [True, False]
    assert remaining is True
    assert second.snapshot().thread_id == before.thread_id
    assert second.snapshot().outstanding == 0
    assert second.snapshot().handles == 0


@pytest.mark.asyncio
async def test_cel_operational_errors_have_no_implicit_context() -> None:
    from kora.cel_eval.thread import Evaluator

    evaluator = Evaluator(clock)
    await evaluator.close()
    caught: EvaluatorUnavailable | None = None

    try:
        raise ValueError("sensitive-caller-marker")
    except ValueError:
        try:
            await evaluator.evaluate(expression("true"), {}, {}, ErrorContext("when"))
        except EvaluatorUnavailable as error:
            caught = error

    assert caught is not None
    assert caught.reason is UnavailableReason.CLOSED
    assert caught.__cause__ is None
    assert caught.__context__ is None


@pytest.mark.parametrize(
    "scenario",
    [
        "priority_cancel_close",
        "close_timeout",
        "batch_limits",
    ],
)
def test_cel_evaluation_thread_priority_close_and_cancellation(scenario: str) -> None:
    code = (
        "from tests.cel_eval.thread_scenarios import run\n"
        f"run({scenario!r})\n"
        "print('complete')\n"
    )

    result = run_child(code, full_path(ROOT))

    assert result.code == 0, result.stderr
    assert result.stdout.strip() == "complete"
    assert result.stderr == ""


@pytest.mark.parametrize(
    "scenario",
    [
        "fatal_failure",
        "closed_loop",
        "stopped_loop",
        "fatal_closed_loop",
        "control_exception_releases_cache",
    ],
)
def test_cel_evaluation_thread_survives_closed_loops_and_fails_closed(
    scenario: str,
) -> None:
    code = f"from tests.cel_eval.thread_scenarios import run\nrun({scenario!r})\n"

    result = run_child(code, full_path(ROOT))

    assert result.code == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.asyncio
async def test_cel_completed_results_obey_outstanding_byte_limit() -> None:
    from kora.cel_eval.binding import load_binding
    from kora.cel_eval.evaluate import ResultType
    from kora.cel_eval.thread import EvaluationThread, Evaluator, Settings

    owner = EvaluationThread(load_binding(), Settings(outstanding_bytes=32768))
    evaluator = Evaluator(clock, owner)
    from kora.cel import CheckedExpression, check
    from kora.cel.cost import ValueBound, Variable
    from kora.cel.types import STRING

    checked = check(
        "s + s + s + s", {"s": Variable(STRING, ValueBound(4096, 1, 0, 4096))}
    )
    assert isinstance(checked, CheckedExpression)

    with pytest.raises(EvaluatorUnavailable) as caught:
        await evaluator.evaluate(
            checked,
            {"s": "x" * 4094},
            {"s": STRING},
            ErrorContext("reason"),
            ResultType.STRING,
        )
    await evaluator.close()

    assert caught.value.reason is UnavailableReason.BUSY
    assert owner.snapshot().bytes == 0
    assert owner.snapshot().outstanding == 0
