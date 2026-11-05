import asyncio
import gc
from collections.abc import Coroutine
from weakref import ref

import pytest

from kora.api.cel_pattern_validation import IncompletePatternCheck, PreparedPatterns
from kora.api.cel_renderings import IncompleteRenderings, RenderingRefusal
from kora.api.exchange import COMPONENT
from kora.api.graph_cel import (
    GraphCelChecks,
    GraphCelRefusal,
    GraphCelResult,
    verify_graph_cel,
)
from kora.cel_eval.binding import load_binding
from kora.cel_eval.errors import EvaluatorUnavailable, UnavailableReason
from kora.cel_eval.thread import EvaluationThread, Evaluator
from kora.ir.document import GraphDocument
from kora.ir.expression_fields import FieldPath
from kora.ir.wait_expressions import WaitExpressionResult
from kora.observability import Level, LoggingSettings, bound_scope, configure_process
from kora.observability.settings import RenderFormat
from tests.api.cel_pattern_validation_scenarios import (
    PATTERN_MARKER,
    REQUEST_ID,
    assert_idle,
    checked_expression_count,
    observe_deliveries,
    wait_for_delivery,
)
from tests.cel_eval.pattern_validation_scenarios import (
    assert_settled,
    open_evaluator,
    run_loop_turn,
)
from tests.cel_eval.test_cel_evaluation import clock
from tests.ir.approval_default_cases import gate
from tests.ir.approval_input_cases import BUILTINS, unavailable_inputs
from tests.ir.branch_expression_cases import branch, branch_document
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.retry_expression_cases import retrying
from tests.ir.trigger_expression_cases import sensor
from tests.ir.wait_expression_cases import wait

FIELDS: int = 9
DECLARATIONS: int = 13
WORK: int = 100_000
SINGLE_FAMILY_CHARACTERS: int = 100
SINGLE_FAMILY_PATTERN_WORK: int = 160
PATTERN_OFFSET: int = len('"a".matches(')
APPROVAL_PATTERN_OFFSET: int = len('true || "😀".matches(')
REJECTED_OFFSETS: dict[FieldPath, int] = {
    ("graph", "nodes", "branch", "config", "cases", 0, "when"): PATTERN_OFFSET,
    ("graph", "nodes", "gate", "config", "approve_when"): APPROVAL_PATTERN_OFFSET,
    ("graph", "nodes", "retry", "retry", "predicate"): PATTERN_OFFSET,
    ("graph", "subgraphs", "unused", "retry", "predicate"): PATTERN_OFFSET,
    ("graph", "subgraphs", "unused", "nodes", "wait", "config", "poll", "until"): (
        PATTERN_OFFSET
    ),
    ("graph", "triggers", 1, "poll", "until"): PATTERN_OFFSET,
}


def mixed_document(
    *, rejected: bool = False, wait_node: dict[str, object] | None = None
) -> GraphDocument:
    prefix = "(?ii)" if rejected else "^"
    escaped = r"\u0070" + PATTERN_MARKER[1:]
    predicate = f'"a".matches("{prefix}{PATTERN_MARKER}")'
    escaped_predicate = f'true || "😀".matches("{prefix}{escaped}")'
    return document_for(
        contracts=BUILTINS,
        nodes={
            "branch": branch(predicate, '"reason"', '"fallback"'),
            "target": node(depends_on=["branch"]),
            "default": gate(),
            "gate": gate(predicate=escaped_predicate),
            "retry": retrying(predicate),
        },
        bodies={
            "unused": {
                "retry": {"max_attempts": 1, "predicate": predicate},
                "nodes": {
                    "wait": wait(predicate, poll=True)
                    if wait_node is None
                    else wait_node
                },
            }
        },
        triggers=[{"kind": "manual"}, sensor(predicate)],
    )


def verify(
    document: GraphDocument,
    evaluator: Evaluator,
    *,
    declarations: int = DECLARATIONS,
    triggers: int = 2,
    characters: int = WORK,
    pattern_work: int = WORK,
) -> Coroutine[None, None, GraphCelResult]:
    return verify_graph_cel(
        document,
        evaluator,
        max_declarations=declarations,
        max_triggers=triggers,
        max_rendering_characters=characters,
        max_pattern_work=pattern_work,
    )


def check_mixed() -> None:
    accepted = mixed_document()
    rejected = mixed_document(rejected=True)
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            valid = observe_deliveries(
                loop, loop.create_task(verify(accepted, evaluator)), owner, FIELDS + 1
            )
            invalid = observe_deliveries(
                loop, loop.create_task(verify(rejected, evaluator)), owner, FIELDS + 1
            )
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())

    assert isinstance(valid, GraphCelChecks)
    assert valid.compiled_fields == FIELDS
    assert valid.diagnostics.valid
    assert isinstance(invalid, GraphCelChecks)
    assert invalid.compiled_fields == FIELDS
    assert not invalid.diagnostics.valid
    assert len(invalid.diagnostics.errors) == len(REJECTED_OFFSETS)
    assert {item.path: item.offset for item in invalid.diagnostics.errors} == (
        REJECTED_OFFSETS
    )
    assert {item.code for item in invalid.diagnostics.errors} == {"IR011"}
    assert_settled(owner)


def check_refusals() -> None:
    document = mixed_document()
    single_family = branch_document(
        f'"a".matches("^{PATTERN_MARKER}")', '"reason"', '"fallback"'
    )
    invalid_wait = mixed_document(wait_node=wait("unknown", poll=True))
    missing_input = mixed_document(
        wait_node=wait("inputs.unavailable.ok", poll=True, inputs=unavailable_inputs())
    )
    runtime_data = document.model_dump(mode="json")
    runtime_data["runtime"] = {
        "runtime": "local",
        "vcs": "git",
        "infrastructure_retry": {"max_attempts": 1, "predicate": "true"},
    }
    infrastructure = GraphDocument.model_validate(runtime_data)
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        evaluator = runner.run(open_evaluator(owner))
        try:
            prefix = runner.run(
                verify(
                    single_family,
                    evaluator,
                    characters=SINGLE_FAMILY_CHARACTERS,
                    pattern_work=SINGLE_FAMILY_PATTERN_WORK,
                )
            )
        finally:
            runner.run(evaluator.close())
        invalid = runner.run(verify(document, evaluator, pattern_work=0))
        oversized_graph = runner.run(
            verify(document, evaluator, declarations=DECLARATIONS - 1)
        )
        oversized_triggers = runner.run(verify(document, evaluator, triggers=1))
        oversized_patterns = runner.run(
            verify(document, evaluator, pattern_work=SINGLE_FAMILY_PATTERN_WORK)
        )
        oversized_renderings = runner.run(
            verify(document, evaluator, characters=SINGLE_FAMILY_CHARACTERS)
        )
        unavailable_context = runner.run(verify(infrastructure, evaluator))
        typed_refusal = runner.run(verify(invalid_wait, evaluator))
        unresolved = runner.run(verify(missing_input, evaluator))

    assert isinstance(prefix, GraphCelChecks)
    assert prefix.compiled_fields == 3
    assert prefix.diagnostics.valid
    assert invalid is GraphCelRefusal.INVALID_ALLOWANCE
    assert oversized_graph is oversized_triggers is GraphCelRefusal.CAPACITY_EXHAUSTED
    assert oversized_patterns is IncompletePatternCheck.CAPACITY_EXHAUSTED
    assert oversized_renderings == IncompleteRenderings(
        RenderingRefusal.CAPACITY_EXHAUSTED
    )
    assert unavailable_context is GraphCelRefusal.INFRASTRUCTURE_CONTEXT_UNRESOLVED
    assert isinstance(typed_refusal, WaitExpressionResult)
    assert not typed_refusal.wait_expressions_valid
    assert isinstance(unresolved, WaitExpressionResult)
    assert unresolved.context_unresolved == 1
    assert unresolved.diagnostics.valid
    assert not unresolved.wait_expressions_complete
    assert_settled(owner)


def pending_patterns() -> int:
    gc.collect()
    return sum(
        isinstance(value, PreparedPatterns) and bool(value.fields or value.probes)
        for value in gc.get_objects()
    )


def check_lifetime() -> None:
    owner = EvaluationThread(load_binding())
    with asyncio.Runner() as runner:
        evaluator = runner.run(open_evaluator(owner))
        try:
            cancel_delivery(runner, evaluator, prior_deliveries=0)
            cancel_delivery(runner, evaluator, prior_deliveries=FIELDS)
            reused = runner.run(verify(mixed_document(), evaluator))
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())
        document = mixed_document()
        document_ref = ref(document)
        operation = verify(document, evaluator)
        del document
        baseline = checked_expression_count()
        with pytest.raises(EvaluatorUnavailable) as unavailable:
            runner.run(operation)
        assert document_ref() is None
        assert checked_expression_count() == baseline
        assert pending_patterns() == 0

    assert isinstance(reused, GraphCelChecks)
    assert reused.diagnostics.valid
    assert unavailable.value.reason is UnavailableReason.CLOSED
    assert_settled(owner)


def cancel_delivery(
    runner: asyncio.Runner, evaluator: Evaluator, *, prior_deliveries: int
) -> None:
    document = mixed_document()
    document_ref = ref(document)
    baseline = checked_expression_count()
    loop = runner.get_loop()
    owner = evaluator.owner
    task = loop.create_task(verify(document, evaluator))
    del document

    for _ in range(prior_deliveries):
        run_loop_turn(loop)
        wait_for_delivery(owner)
        run_loop_turn(loop)
    run_loop_turn(loop)
    delivery = wait_for_delivery(owner)
    document_released = document_ref() is None
    trees_during_delivery = checked_expression_count() - baseline
    patterns_during_delivery = pending_patterns()
    assert task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        loop.run_until_complete(task)

    assert delivery.outstanding == delivery.deliveries == 1
    assert document_released
    assert trees_during_delivery == (0 if prior_deliveries else FIELDS)
    assert patterns_during_delivery == 1
    assert cancelled.value.__context__ is None
    assert checked_expression_count() == baseline
    assert pending_patterns() == 0
    assert_idle(owner, 1)


def run(scenario: str) -> None:
    cases = {
        "mixed": check_mixed,
        "refusals": check_refusals,
        "lifetime": check_lifetime,
    }
    diagnostics = configure_process(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG, render_format=RenderFormat.JSON),
        "graph-cel-test",
        clock,
    )

    try:
        with bound_scope(request_id=REQUEST_ID):
            cases[scenario]()
    finally:
        closed = diagnostics.shutdown(5)

    assert closed
