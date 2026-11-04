import asyncio
from typing import cast

import pytest

from kora.cel.limits import MAX_CODE_POINTS, MAX_PATTERN_NESTING
from kora.cel.parser import RENDERING_LIMITS, parse
from kora.cel.render import render_string
from kora.cel.tree import Call, StringLiteral
from kora.cel_eval import CEL_COMPONENT
from kora.cel_eval.binding import PatternProbe, load_binding
from kora.cel_eval.errors import EvaluatorUnavailable, UnavailableReason
from kora.cel_eval.evaluate import PatternCompilation
from kora.cel_eval.thread import EvaluationThread, Evaluator, Settings
from kora.observability import Level, LoggingSettings, configure_process
from tests.cel_eval.test_cel_conversion import expression
from tests.cel_eval.test_cel_evaluation import clock

PRIVATE_PATTERN_MARKER: str = "private-authored-regex-marker-712"
EXPANDED_PATTERN: str = "😀" * 450


async def open_evaluator(owner: EvaluationThread) -> Evaluator:
    return Evaluator(clock, owner)


def run_loop_turn(loop: asyncio.AbstractEventLoop) -> None:
    loop.call_soon(loop.stop)
    loop.run_forever()


def assert_settled(owner: EvaluationThread) -> None:
    snapshot = owner.snapshot()
    assert not snapshot.failed
    assert snapshot.handles == snapshot.outstanding == snapshot.bytes == 0
    assert snapshot.queued == snapshot.running == snapshot.deliveries == 0
    assert snapshot.cache_entries == snapshot.cache_characters == 0


async def native_results() -> None:
    binding = load_binding()
    rejected_pattern = f"(?ii){PRIVATE_PATTERN_MARKER}"
    escaped_pattern = '"\\\\é\x00\n\r\t'
    escaped = PatternProbe(escaped_pattern)
    checked = expression(f'"".matches({render_string(rejected_pattern)})')
    binding.compile(checked)
    raw = cast(PatternProbe, f'"".matches("{PRIVATE_PATTERN_MARKER}")')
    evaluator = Evaluator(clock)

    try:
        with pytest.raises(
            TypeError, match="native pattern compilation requires a checked probe"
        ):
            binding.compile_pattern_probe(raw)
        truth_values = [
            binding.execute(binding.compile_pattern_probe(PatternProbe(pattern)), {})
            for pattern in ("", "^a$")
        ]
        escaped_tree = parse(escaped.rendering, RENDERING_LIMITS)
        results = [
            await evaluator.validate_pattern(pattern)
            for pattern in ("", "^a$", escaped_pattern, rejected_pattern)
        ]
    finally:
        await evaluator.close()

    assert truth_values == [True, False]
    assert escaped_tree == Call(
        "matches", StringLiteral("", 0), (StringLiteral(escaped_pattern, 0),), 0
    )
    assert results == [
        PatternCompilation.ACCEPTED,
        PatternCompilation.ACCEPTED,
        PatternCompilation.ACCEPTED,
        PatternCompilation.REJECTED,
    ]
    assert PRIVATE_PATTERN_MARKER not in repr(results)
    assert_settled(evaluator.owner)
    with evaluator.owner.condition:
        assert evaluator.owner.condition.wait_for(
            lambda: evaluator.snapshot().diagnostics == 0, 5
        )


def check_native_results() -> None:
    diagnostics = configure_process(
        CEL_COMPONENT, LoggingSettings(level=Level.DEBUG), "native-pattern-test", clock
    )

    try:
        asyncio.run(native_results())
    finally:
        closed = diagnostics.shutdown(5)

    assert closed


async def static_admission() -> None:
    refused_patterns = (
        "a" * (MAX_CODE_POINTS + 1),
        "\ud800",
        "[",
        "(?=a)",
        "(" * (MAX_PATTERN_NESTING + 1) + "a" + ")" * (MAX_PATTERN_NESTING + 1),
        "a{40000}",
    )
    refused_owner = EvaluationThread(load_binding(), Settings(outstanding_bytes=1))
    refused_evaluator = Evaluator(clock, refused_owner)
    evaluator = Evaluator(clock)

    try:
        refused = [
            await refused_evaluator.validate_pattern(pattern)
            for pattern in refused_patterns
        ]
        accepted = await evaluator.validate_pattern(EXPANDED_PATTERN)
    finally:
        await refused_evaluator.close()
        await evaluator.close()

    assert refused == [PatternCompilation.INCOMPLETE] * len(refused_patterns)
    assert len(f'"".matches({render_string(EXPANDED_PATTERN)})') > MAX_CODE_POINTS
    assert accepted is PatternCompilation.ACCEPTED
    assert_settled(refused_owner)
    assert_settled(evaluator.owner)


def check_static_admission() -> None:
    asyncio.run(static_admission())


def shared_compile_limit(settings: Settings) -> None:
    owner = EvaluationThread(load_binding(), settings)
    checked = expression("true")

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        first = runner.run(open_evaluator(owner))
        second = runner.run(open_evaluator(owner))
        pending = loop.create_task(first.validate_pattern(""))
        try:
            with owner.condition:
                run_loop_turn(loop)
                competing = loop.create_task(second.validate(checked))
                run_loop_turn(loop)
                queued = owner.snapshot()
                assert competing.done()
                with pytest.raises(EvaluatorUnavailable) as busy:
                    competing.result()
            result = loop.run_until_complete(pending)
            rendering = runner.run(second.validate(checked))
        finally:
            runner.run(first.close())
            runner.run(second.close())

    assert queued.queued == queued.outstanding == 1
    assert busy.value.reason is UnavailableReason.BUSY
    assert result is PatternCompilation.ACCEPTED
    assert rendering.valid
    assert_settled(owner)


def check_shared_compile_limits() -> None:
    shared_compile_limit(Settings(compile_jobs=1))
    shared_compile_limit(Settings(compile_jobs=2, outstanding_bytes=1_024))


def initial_charge(
    pattern: str, budget: int, expected: PatternCompilation | UnavailableReason
) -> None:
    owner = EvaluationThread(load_binding(), Settings(outstanding_bytes=budget))

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        pending = loop.create_task(evaluator.validate_pattern(pattern))
        try:
            with owner.condition:
                run_loop_turn(loop)
                admitted = owner.snapshot()
            try:
                outcome: PatternCompilation | UnavailableReason = (
                    loop.run_until_complete(pending)
                )
            except EvaluatorUnavailable as refused:
                outcome = refused.reason
        finally:
            runner.run(evaluator.close())

    assert outcome is expected
    if expected is PatternCompilation.ACCEPTED:
        assert admitted.queued == admitted.outstanding == 1
        assert admitted.bytes == budget
    else:
        assert admitted.queued == admitted.outstanding == admitted.bytes == 0
    assert_settled(owner)


def check_initial_charge() -> None:
    expanded_charge = 4 * len(f'"".matches({render_string(EXPANDED_PATTERN)})')

    initial_charge("", 1_024, PatternCompilation.ACCEPTED)
    initial_charge("", 1_023, UnavailableReason.BUSY)
    initial_charge(EXPANDED_PATTERN, expanded_charge, PatternCompilation.ACCEPTED)
    initial_charge(EXPANDED_PATTERN, expanded_charge - 1, UnavailableReason.BUSY)


def check_cancellation() -> None:
    owner = EvaluationThread(load_binding())
    expected_charge = 4 * len(f'"".matches({render_string(EXPANDED_PATTERN)})')

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        first = runner.run(open_evaluator(owner))
        second = runner.run(open_evaluator(owner))
        pending = loop.create_task(first.validate_pattern(EXPANDED_PATTERN))

        try:
            run_loop_turn(loop)
            with owner.condition:
                assert owner.condition.wait_for(
                    lambda: owner.snapshot().deliveries == 1, 5
                )
            delivery = owner.snapshot()
            assert pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                loop.run_until_complete(pending)
            runner.run(first.close())
            with pytest.raises(EvaluatorUnavailable) as closed:
                runner.run(first.validate_pattern(""))
            sibling = runner.run(second.validate_pattern(""))
        finally:
            runner.run(first.close())
            runner.run(second.close())

    assert delivery.deliveries == delivery.outstanding == 1
    assert delivery.bytes == expected_charge
    assert pending.cancelled()
    assert closed.value.reason is UnavailableReason.CLOSED
    assert sibling is PatternCompilation.ACCEPTED
    assert_settled(owner)
