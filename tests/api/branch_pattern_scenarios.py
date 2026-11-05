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
    validate_branch_patterns,
)
from kora.api.exchange import COMPONENT
from kora.cel_eval.binding import load_binding
from kora.cel_eval.evaluate import PatternCompilation
from kora.cel_eval.thread import EvaluationThread
from kora.ir.branch_expressions import (
    BranchExpressionChecks,
    BranchExpressionResult,
    checked_branch_sites,
)
from kora.ir.document import GraphDocument
from kora.ir.expression_contexts import InputParameterContexts
from kora.ir.json import JsonObject
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
from tests.ir.branch_expression_cases import SCHEMA, branch
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for


def inventory_document() -> tuple[GraphDocument, tuple[str, str, str]]:
    escaped = r"\u0070" + PATTERN_MARKER[1:]
    when = f'false && "x".matches("(?ii){PATTERN_MARKER}")'
    reason = f'"😀".matches("(?ii){escaped}") ? "yes" : "no"'
    body = f'inputs.data.ok == 1 && "x".matches("(?ii){PATTERN_MARKER}")'
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
            "branch": node(
                "branch",
                inputs=inputs,
                config={
                    "cases": [
                        {"when": "inputs.data.ok && params.ok", "to": ["target"]},
                        {"when": when, "to": ["target"], "reason": reason},
                    ],
                    "else_reason": '"x".matches("^x$") ? "valid" : "invalid"',
                },
            ),
            "target": node(depends_on=["branch"]),
            "call": node("subgraph", config={"body": "recursive"}),
        },
        bodies={
            "unused": {
                "nodes": {
                    "producer": producer(schema=body_schema),
                    "branch": branch(body, '"reason"', inputs=inputs),
                    "target": node(depends_on=["branch"]),
                }
            },
            "recursive": {
                "nodes": {
                    "branch": branch(),
                    "again": node("subgraph", config={"body": "recursive"}),
                }
            },
        },
    )
    return document, (when, reason, body)


def check_inventory() -> None:
    document, sources = inventory_document()
    original = document.model_dump(mode="json")
    sites = checked_branch_sites(document, max_declarations=20)
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            result = observe_deliveries(
                loop,
                loop.create_task(
                    validate_branch_patterns(
                        document, evaluator, max_declarations=20, max_work=WORK
                    )
                ),
                owner,
                2,
            )
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())

    assert isinstance(sites, tuple)
    assert len(sites) == 7
    assert [site.path for site in sites[:4]] == [
        ("graph", "nodes", "branch", "config", "cases", 0, "when"),
        ("graph", "nodes", "branch", "config", "cases", 1, "when"),
        ("graph", "nodes", "branch", "config", "cases", 1, "reason"),
        ("graph", "nodes", "branch", "config", "else_reason"),
    ]
    assert PATTERN_MARKER not in repr(sites)
    assert document.model_dump(mode="json") == original
    assert isinstance(result, CompletePatternCheck)
    assert [(error.path, error.offset) for error in result.diagnostics.errors] == [
        (sites[2].path, sources[1].index('"(?ii)')),
        (sites[1].path, sources[0].index('"(?ii)')),
        (sites[4].path, sources[2].index('"(?ii)')),
    ]
    assert all(error.code == "IR011" for error in result.diagnostics.errors)
    assert_settled(owner)


@contextmanager
def observe_context_construction() -> Generator[list[str]]:
    constructions: list[str] = []
    previous = sys.getprofile()
    constructors = {
        BranchExpressionChecks.__init__.__code__,
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
        nodes={"first": branch('"x".matches("^x$")')},
        bodies={"unused": {"nodes": {"failure": failure}}},
    )


def check_refusals() -> None:
    admitted, _ = inventory_document()
    cases = (
        (admitted, 19, WORK),
        (admitted, -1, WORK),
        (admitted, True, WORK),
        (admitted, 20, 1),
        (failure_document(branch("1")), 6, WORK),
        (failure_document(branch("true &&")), 6, WORK),
        (
            failure_document(branch("inputs.missing", inputs=unavailable_inputs())),
            6,
            WORK,
        ),
        (failure_document(branch("params.ok"), schema=True), 6, WORK),
        (document_for(), 2, WORK),
    )
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            refused = tuple(
                loop.create_task(
                    validate_branch_patterns(
                        graph, evaluator, max_declarations=limit, max_work=work
                    )
                )
                for graph, limit, work in cases[:3]
            )
            with observe_context_construction() as constructions:
                run_loop_turn(loop)
            tasks = tuple(
                loop.create_task(
                    validate_branch_patterns(
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
        isinstance(result, BranchExpressionResult)
        for result in (typed, syntax, context, schema)
    )
    assert isinstance(typed, BranchExpressionResult)
    assert typed.branch_expressions_complete
    assert len(typed.diagnostics.errors) == 1
    assert typed.diagnostics.errors[0].path == (
        "graph",
        "subgraphs",
        "unused",
        "nodes",
        "failure",
        "config",
        "cases",
        0,
        "when",
    )
    assert isinstance(syntax, BranchExpressionResult)
    assert syntax.syntax_unresolved == 1
    assert isinstance(context, BranchExpressionResult)
    assert context.context_unresolved == 1
    assert isinstance(schema, BranchExpressionResult)
    assert schema.schema_unresolved == 1
    assert all(
        not result.branch_expressions_valid
        for result in (typed, syntax, context, schema)
    )
    empty = results[8]
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
            validate_branch_patterns(
                document, evaluator, max_declarations=20, max_work=WORK
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
        "branch-pattern-test",
        clock,
    )

    try:
        with bound_scope(request_id=REQUEST_ID):
            cases[scenario]()
    finally:
        closed = diagnostics.shutdown(5)

    assert closed
