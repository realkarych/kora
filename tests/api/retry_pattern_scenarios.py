import asyncio
import sys
from collections.abc import Generator
from contextlib import contextmanager
from types import FrameType
from weakref import ref

import pytest

from kora.api.cel_pattern_validation import (
    CompletePatternCheck,
    IncompletePatternCheck,
    validate_retry_patterns,
)
from kora.api.exchange import COMPONENT
from kora.cel_eval.binding import load_binding
from kora.cel_eval.evaluate import PatternCompilation
from kora.cel_eval.thread import EvaluationThread
from kora.ir.document import GraphDocument
from kora.ir.expression_contexts import InputParameterContexts
from kora.ir.json import JsonObject
from kora.ir.retry_expressions import (
    RetryExpressionChecks,
    RetryExpressionResult,
    checked_retry_sites,
)
from kora.observability import Level, LoggingSettings, bound_scope, configure_process
from kora.observability.settings import RenderFormat
from tests.api.cel_pattern_validation_scenarios import (
    PATTERN_MARKER,
    REQUEST_ID,
    WORK,
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
from tests.ir.approval_input_cases import BUILTINS, producer, unavailable_inputs
from tests.ir.branch_expression_cases import SCHEMA
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.retry_expression_cases import retrying


def inventory_document() -> tuple[GraphDocument, tuple[str, str, str]]:
    escaped = r"\u0070" + PATTERN_MARKER[1:]
    root = (
        "has(outputs.value) && outputs.value.ok && inputs.data.ok && params.ok"
        f' && false && "x".matches("(?ii){PATTERN_MARKER}")'
    )
    body = f'params.ok && "😀".matches("(?ii){escaped}")'
    child = (
        "has(outputs.value) && outputs.value.ok == 1"
        f' && "x".matches("(?ii){PATTERN_MARKER}")'
    )
    inputs = {
        "data": {"type": "StructuredOutput", "from": "producer", "output": "value"}
    }
    body_schema: JsonObject = {
        "properties": {"ok": {"type": "integer"}},
        "required": ["ok"],
    }
    document = document_for(
        contracts=BUILTINS,
        params=SCHEMA,
        nodes={
            "producer": producer(schema=SCHEMA),
            "check": retrying(root, producer(schema=SCHEMA), inputs=inputs),
            "omitted": retrying(None),
            "call": node("subgraph", config={"body": "recursive"}),
        },
        bodies={
            "unused": {
                "retry": {"max_attempts": 1, "predicate": body},
                "nodes": {
                    "check": retrying(child, producer(schema=body_schema)),
                    "timer": node(),
                },
            },
            "recursive": {
                "retry": {"max_attempts": 1, "predicate": "true"},
                "nodes": {
                    "check": retrying('"x".matches("^x$")'),
                    "again": node("subgraph", config={"body": "recursive"}),
                },
            },
        },
    )
    return document, (root, body, child)


def check_inventory() -> None:
    document, sources = inventory_document()
    original = document.model_dump(mode="json")
    sites = checked_retry_sites(document, max_declarations=15)
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            result = observe_deliveries(
                loop,
                loop.create_task(
                    validate_retry_patterns(
                        document, evaluator, max_declarations=15, max_work=WORK
                    )
                ),
                owner,
                2,
            )
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())

    assert isinstance(sites, tuple)
    assert len(sites) == 5
    assert [site.path for site in sites] == [
        ("graph", "nodes", "check", "retry", "predicate"),
        ("graph", "subgraphs", "unused", "retry", "predicate"),
        ("graph", "subgraphs", "unused", "nodes", "check", "retry", "predicate"),
        ("graph", "subgraphs", "recursive", "retry", "predicate"),
        ("graph", "subgraphs", "recursive", "nodes", "check", "retry", "predicate"),
    ]
    assert PATTERN_MARKER not in repr(sites)
    assert document.model_dump(mode="json") == original
    assert isinstance(result, CompletePatternCheck)
    assert [(error.path, error.offset) for error in result.diagnostics.errors] == [
        (sites[0].path, sources[0].index('"(?ii)')),
        (sites[2].path, sources[2].index('"(?ii)')),
        (sites[1].path, sources[1].index('"(?ii)')),
    ]
    assert all(error.code == "IR011" for error in result.diagnostics.errors)
    assert_settled(owner)


@contextmanager
def observe_context_construction() -> Generator[list[str]]:
    constructions: list[str] = []
    previous = sys.getprofile()
    constructors = {
        RetryExpressionChecks.__init__.__code__,
        InputParameterContexts.__init__.__code__,
    }

    def observe(frame: FrameType, event: str, argument: object) -> None:
        if event == "call" and frame.f_code in constructors:
            constructions.append(frame.f_code.co_qualname)

    try:
        sys.setprofile(observe)
        yield constructions
    finally:
        sys.setprofile(previous)


def failure_document(
    failure: dict[str, object], *, schema: bool = False
) -> GraphDocument:
    return document_for(
        params={"$ref": "https://example.invalid/private-schema"} if schema else None,
        nodes={"first": retrying('"x".matches("^x$")')},
        bodies={"unused": {"nodes": {"failure": failure}}},
    )


def check_refusals() -> None:
    admitted, _ = inventory_document()
    cases = (
        (admitted, 14, WORK),
        (admitted, -1, WORK),
        (admitted, True, WORK),
        (admitted, 15, 1),
        (failure_document(retrying("1")), 4, WORK),
        (failure_document(retrying("true &&")), 4, WORK),
        (
            failure_document(retrying("inputs.missing", inputs=unavailable_inputs())),
            4,
            WORK,
        ),
        (failure_document(retrying("params.ok"), schema=True), 4, WORK),
        (
            document_for(
                nodes={"first": retrying('"x".matches("^x$")')},
                bodies={
                    "unused": {
                        "retry": {"max_attempts": 1, "predicate": "has(outputs.value)"},
                        "nodes": {"timer": node()},
                    }
                },
            ),
            4,
            WORK,
        ),
        (document_for(), 2, WORK),
    )
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            refused = tuple(
                loop.create_task(
                    validate_retry_patterns(
                        graph, evaluator, max_declarations=limit, max_work=work
                    )
                )
                for graph, limit, work in cases[:3]
            )
            with observe_context_construction() as constructions:
                run_loop_turn(loop)
            tasks = tuple(
                loop.create_task(
                    validate_retry_patterns(
                        graph, evaluator, max_declarations=limit, max_work=work
                    )
                )
                for graph, limit, work in cases[3:]
            )
            run_loop_turn(loop)
            assert all(task.done() for task in (*refused, *tasks))
            results = tuple(task.result() for task in (*refused, *tasks))
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())

    assert constructions == []
    assert results[:4] == (
        IncompletePatternCheck.CAPACITY_EXHAUSTED,
        IncompletePatternCheck.INVALID_INVENTORY,
        IncompletePatternCheck.INVALID_INVENTORY,
        IncompletePatternCheck.CAPACITY_EXHAUSTED,
    )
    typed, syntax, context, schema = results[4:8]
    assert all(
        isinstance(result, RetryExpressionResult)
        for result in (typed, syntax, context, schema)
    )
    assert isinstance(typed, RetryExpressionResult)
    assert typed.retry_expressions_complete
    assert len(typed.diagnostics.errors) == 1
    assert typed.diagnostics.errors[0].path == (
        "graph",
        "subgraphs",
        "unused",
        "nodes",
        "failure",
        "retry",
        "predicate",
    )
    assert isinstance(syntax, RetryExpressionResult)
    assert syntax.syntax_unresolved == 1
    assert isinstance(context, RetryExpressionResult)
    assert context.context_unresolved == 1
    assert isinstance(schema, RetryExpressionResult)
    assert schema.schema_unresolved == 1
    assert all(
        not result.retry_expressions_valid
        for result in (typed, syntax, context, schema)
    )
    body = results[8]
    assert isinstance(body, RetryExpressionResult)
    assert body.context_unresolved == 1
    assert not body.retry_expressions_complete
    empty = results[9]
    assert isinstance(empty, CompletePatternCheck)
    assert empty.diagnostics.valid
    assert_settled(owner)


def check_cancellation() -> None:
    document, _ = inventory_document()
    document_ref = ref(document)
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        baseline = checked_expression_count()
        task = loop.create_task(
            validate_retry_patterns(
                document, evaluator, max_declarations=15, max_work=WORK
            )
        )
        del document
        try:
            run_loop_turn(loop)
            delivery = wait_for_delivery(owner)
            assert document_ref() is None
            assert checked_expression_count() == baseline
            assert task.cancel()
            with pytest.raises(asyncio.CancelledError):
                loop.run_until_complete(task)
            assert_idle(owner, 1)
            assert (
                runner.run(evaluator.validate_pattern(""))
                is PatternCompilation.ACCEPTED
            )
        finally:
            runner.run(evaluator.close())

    assert delivery.outstanding == delivery.deliveries == 1
    assert task.cancelled()
    assert_settled(owner)


def run(scenario: str) -> None:
    cases = {
        "inventory": check_inventory,
        "refusals": check_refusals,
        "cancellation": check_cancellation,
    }
    diagnostics = configure_process(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG, render_format=RenderFormat.JSON),
        "retry-pattern-test",
        clock,
    )

    try:
        with bound_scope(request_id=REQUEST_ID):
            cases[scenario]()
    finally:
        closed = diagnostics.shutdown(5)

    assert closed
