import asyncio
import gc
from typing import TypeVar
from weakref import ReferenceType, ref

import pytest

from kora.api.cel_pattern_validation import (
    CheckedPatternSite,
    CompletePatternCheck,
    IncompletePatternCheck,
    validate_cel_patterns,
)
from kora.api.exchange import COMPONENT
from kora.cel import CheckedExpression, Environment, ResultKind, check_typed
from kora.cel.render import render_string
from kora.cel.tree import Node
from kora.cel_eval.binding import load_binding
from kora.cel_eval.errors import EvaluatorUnavailable, UnavailableReason
from kora.cel_eval.evaluate import PatternCompilation
from kora.cel_eval.thread import EvaluationThread, Settings, Snapshot
from kora.ir.diagnostics import MAX_INTEGER
from kora.ir.expression_fields import FieldPath
from kora.observability import Level, LoggingSettings, bound_scope, configure_process
from kora.observability.settings import RenderFormat
from tests.cel_eval.pattern_validation_scenarios import (
    EXPANDED_PATTERN,
    assert_settled,
    open_evaluator,
    run_loop_turn,
)
from tests.cel_eval.test_cel_evaluation import clock

WORK: int = 100_000
PATTERN_MARKER: str = "private-pattern-marker-723"
PATH_MARKER: str = "private-malformed-path-marker-723"
REQUEST_ID: str = "req_cel_pattern_inventory"
T = TypeVar("T")


def site_path(name: str) -> FieldPath:
    return ("graph", "nodes", name, "config", "approve_when")


def typed_site(
    path: FieldPath, source: str, *, authored: bool = True
) -> CheckedPatternSite:
    checked = check_typed(source, Environment({}, ResultKind.BOOL))
    assert isinstance(checked, CheckedExpression)
    return CheckedPatternSite(path, checked, authored)


def wait_for_delivery(owner: EvaluationThread) -> Snapshot:
    with owner.condition:
        assert owner.condition.wait_for(lambda: owner.snapshot().deliveries == 1, 5)
        return owner.snapshot()


def assert_idle(owner: EvaluationThread, handles: int) -> None:
    snapshot = owner.snapshot()
    assert not snapshot.failed
    assert snapshot.handles == handles
    assert snapshot.outstanding == snapshot.bytes == 0
    assert snapshot.queued == snapshot.running == snapshot.deliveries == 0
    assert snapshot.cache_entries == snapshot.cache_characters == 0


def observe_deliveries(
    loop: asyncio.AbstractEventLoop,
    task: asyncio.Task[T],
    owner: EvaluationThread,
    expected: int,
) -> T:
    for _ in range(expected):
        run_loop_turn(loop)
        assert not task.done()
        delivery = wait_for_delivery(owner)
        assert delivery.outstanding == 1
        run_loop_turn(loop)
    run_loop_turn(loop)
    assert task.done()
    return task.result()


def check_deduplication() -> None:
    escaped_marker = r"\u0070" + PATTERN_MARKER[1:]
    examples = (
        ("lazy", f'false && "a".matches("(?ii){PATTERN_MARKER}")', PATTERN_MARKER),
        ("escaped", f'true || "a".matches("(?ii){escaped_marker}")', escaped_marker),
        (
            "receiver",
            '("😀".matches("(?ii)inner") ? "y" : "n").matches("(?ii)outer")',
            "inner",
        ),
        (
            "conditional",
            'true ? !"a".matches("(?ii)then") : "a".matches("(?ii)else")',
            "then",
        ),
        ("has", 'has(dyn({"ok": "a".matches("(?ii)has")}).ok)', "has"),
        ("index", '["z"]["a".matches("(?ii)index") ? 0 : 1] == "z"', "index"),
        ("map_key", '{"a".matches("(?ii)key"): 1}[false] == 1', "key"),
        ("range", '["a".matches("(?ii)range")].all(x, x)', "range"),
        (
            "filtered_map",
            'size(["a"].map(x, x.matches("(?ii)filter"), '
            'x.matches("(?ii)transform"))) == 1',
            "filter",
        ),
    )
    authored = tuple(
        typed_site(site_path(name), source) for name, source, _ in examples
    )
    sites = (*authored, typed_site(site_path("valid"), '"a".matches("^a$")'))
    expected = {
        site_path(name): source.index(f'"(?ii){literal}')
        for name, source, literal in examples
    }
    shared = typed_site(site_path("shared"), '"a".matches("(?ii)a")').checked
    crowded = tuple(
        CheckedPatternSite(site_path(f"gate_{index:04}"), shared, True)
        for index in range(1001)
    )
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            first = observe_deliveries(
                loop,
                loop.create_task(
                    validate_cel_patterns(sites, evaluator, max_work=WORK)
                ),
                owner,
                12,
            )
            repeated = observe_deliveries(
                loop,
                loop.create_task(
                    validate_cel_patterns(sites, evaluator, max_work=WORK)
                ),
                owner,
                12,
            )
            bounded = observe_deliveries(
                loop,
                loop.create_task(
                    validate_cel_patterns(crowded, evaluator, max_work=WORK)
                ),
                owner,
                1,
            )
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())

    assert isinstance(first, CompletePatternCheck)
    assert repeated == first
    assert first.diagnostics.errors_omitted == 0
    assert len(first.diagnostics.errors) == len(expected)
    assert {entry.path: entry.offset for entry in first.diagnostics.errors} == expected
    assert all(
        entry.code == "IR011" and entry.message == "invalid regular expression"
        for entry in first.diagnostics.errors
    )
    assert PATTERN_MARKER not in repr(first)
    assert isinstance(bounded, CompletePatternCheck)
    assert len(bounded.diagnostics.errors) == 1_000
    assert bounded.diagnostics.errors_omitted == 1
    assert not bounded.diagnostics.valid
    assert_settled(owner)


def assert_preflight_refusal(
    loop: asyncio.AbstractEventLoop,
    task: asyncio.Task[T],
    owner: EvaluationThread,
    expected: IncompletePatternCheck,
) -> None:
    with owner.condition:
        run_loop_turn(loop)
        assert task.done()
        assert task.result() is expected
        assert_idle(owner, 1)


def check_refusals() -> None:
    first_source = 'false && "a".matches("(?ii)a")'
    second_source = r'true || "a".matches("(?ii)\u0061")'
    first = typed_site(site_path("first"), first_source)
    second = typed_site(site_path("second"), second_source)
    malformed = CheckedPatternSite(("graph", PATH_MARKER * 16), first.checked, True)
    large_index = CheckedPatternSite(("graph", MAX_INTEGER + 1), first.checked, True)
    empty = CheckedPatternSite((), first.checked, True)
    generated = CheckedPatternSite(second.path, second.checked, False)
    expanded = typed_site(site_path("expanded"), f'"x".matches("{EXPANDED_PATTERN}")')
    exact = sum(
        1
        + len(path)
        + sum(len(segment) for segment in path if isinstance(segment, str))
        + 5
        + 1
        + len("(?ii)a")
        for path in (first.path, second.path)
    )
    owner = EvaluationThread(load_binding(), Settings(outstanding_bytes=1_024))

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            for inventory, allowance, reason in (
                ((first, second), exact - 1, IncompletePatternCheck.CAPACITY_EXHAUSTED),
                ((first, first), WORK, IncompletePatternCheck.INVALID_INVENTORY),
                ((first, malformed), WORK, IncompletePatternCheck.INVALID_INVENTORY),
                ((first, large_index), WORK, IncompletePatternCheck.INVALID_INVENTORY),
                ((first, empty), WORK, IncompletePatternCheck.INVALID_INVENTORY),
            ):
                assert_preflight_refusal(
                    loop,
                    loop.create_task(
                        validate_cel_patterns(inventory, evaluator, max_work=allowance)
                    ),
                    owner,
                    reason,
                )
            complete = observe_deliveries(
                loop,
                loop.create_task(
                    validate_cel_patterns((first, second), evaluator, max_work=exact)
                ),
                owner,
                1,
            )
            incomplete = observe_deliveries(
                loop,
                loop.create_task(
                    validate_cel_patterns((first, generated), evaluator, max_work=WORK)
                ),
                owner,
                1,
            )
            with pytest.raises(EvaluatorUnavailable) as busy:
                observe_deliveries(
                    loop,
                    loop.create_task(
                        validate_cel_patterns(
                            (first, expanded), evaluator, max_work=WORK
                        )
                    ),
                    owner,
                    1,
                )
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())
        with pytest.raises(EvaluatorUnavailable) as closed:
            runner.run(validate_cel_patterns((first,), evaluator, max_work=WORK))

    assert isinstance(complete, CompletePatternCheck)
    assert {entry.path for entry in complete.diagnostics.errors} == {
        first.path,
        second.path,
    }
    assert incomplete is IncompletePatternCheck.GENERATED_REJECTED
    assert busy.value.reason is UnavailableReason.BUSY
    assert closed.value.reason is UnavailableReason.CLOSED
    assert PATH_MARKER not in repr(incomplete) + repr(busy.value) + repr(closed.value)
    assert_settled(owner)


def cancellation_site() -> tuple[tuple[CheckedPatternSite, ...], ReferenceType[Node]]:
    site = typed_site(site_path("cancelled"), f'"a".matches("{EXPANDED_PATTERN}")')
    return (site,), ref(site.checked.tree)


def check_cancellation() -> None:
    owner = EvaluationThread(load_binding())
    sites, tree = cancellation_site()
    expected_charge = 4 * len(f'"".matches({render_string(EXPANDED_PATTERN)})')

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        sibling = runner.run(open_evaluator(owner))
        task = loop.create_task(validate_cel_patterns(sites, evaluator, max_work=WORK))
        del sites
        try:
            run_loop_turn(loop)
            delivery = wait_for_delivery(owner)
            assert tree() is None
            assert task.cancel()
            with pytest.raises(asyncio.CancelledError):
                loop.run_until_complete(task)
            assert_idle(owner, 2)
            original_result = runner.run(evaluator.validate_pattern(""))
            sibling_result = runner.run(sibling.validate_pattern(""))
            assert_idle(owner, 2)
        finally:
            runner.run(evaluator.close())
            runner.run(sibling.close())

    assert delivery.bytes == expected_charge
    assert delivery.outstanding == delivery.deliveries == 1
    assert original_result is sibling_result is PatternCompilation.ACCEPTED
    assert task.cancelled()
    assert_settled(owner)


def checked_expression_count() -> int:
    gc.collect()
    return sum(isinstance(value, CheckedExpression) for value in gc.get_objects())


def run(scenario: str) -> None:
    cases = {
        "deduplication": check_deduplication,
        "refusals": check_refusals,
        "cancellation": check_cancellation,
    }
    diagnostics = configure_process(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG, render_format=RenderFormat.JSON),
        "cel-pattern-inventory-test",
        clock,
    )

    try:
        with bound_scope(request_id=REQUEST_ID):
            cases[scenario]()
    finally:
        closed = diagnostics.shutdown(5)

    assert closed
