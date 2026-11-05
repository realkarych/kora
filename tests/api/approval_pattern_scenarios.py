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
    validate_approval_patterns,
)
from kora.api.exchange import COMPONENT
from kora.cel_eval.binding import load_binding
from kora.cel_eval.evaluate import PatternCompilation
from kora.cel_eval.thread import EvaluationThread
from kora.ir.approval_compilation import ApprovalPrerequisite, checked_approval_sites
from kora.ir.approval_defaults import ApprovalDefaultChecks
from kora.ir.approval_predicates import ApprovalPredicateChecks
from kora.ir.diagnostic_collection import DiagnosticCollection
from kora.ir.document import GraphDocument
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
from tests.ir.approval_default_cases import gate
from tests.ir.approval_input_cases import BUILTINS, producer, unavailable_inputs
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for


def inventory_document() -> tuple[GraphDocument, tuple[str, str]]:
    schema: JsonObject = {
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    body_schema: JsonObject = {
        "properties": {"name": {"type": "integer"}},
        "required": ["name"],
    }
    inputs = {
        "data": {"type": "StructuredOutput", "from": "producer", "output": "value"}
    }
    escaped = r"\u0070" + PATTERN_MARKER[1:]
    root = (
        'response.decision == "approve" && '
        f'inputs.data.name.matches("(?ii){PATTERN_MARKER}") && '
        'params.name.matches("^a$")'
    )
    body = f'inputs.data.name == 1 && (true || "😀".matches("(?ii){escaped}"))'
    document = document_for(
        contracts=BUILTINS,
        params=schema,
        nodes={
            "producer": producer(schema=schema),
            "default": gate(),
            "gate": gate(predicate=root, inputs=inputs),
            "call": node("subgraph", config={"body": "recursive"}),
        },
        bodies={
            "unused": {
                "nodes": {
                    "producer": producer(schema=body_schema),
                    "gate": gate(predicate=body, inputs=inputs),
                }
            },
            "recursive": {
                "nodes": {
                    "gate": gate(predicate='"a".matches("^a$")'),
                    "again": node("subgraph", config={"body": "recursive"}),
                }
            },
        },
    )
    return document, (root, body)


def check_inventory() -> None:
    document, sources = inventory_document()
    original = document.model_dump(mode="json")
    sites = checked_approval_sites(document, max_declarations=15)
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            result = observe_deliveries(
                loop,
                loop.create_task(
                    validate_approval_patterns(
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
    assert [site.authored for site in sites] == [False, True, True, True]
    assert [site.path for site in sites] == [
        ("graph", "nodes", "default", "config", "approve_when"),
        ("graph", "nodes", "gate", "config", "approve_when"),
        ("graph", "subgraphs", "unused", "nodes", "gate", "config", "approve_when"),
        ("graph", "subgraphs", "recursive", "nodes", "gate", "config", "approve_when"),
    ]
    assert sites[0].checked.rendering == '(response.decision == "approve")'
    assert PATTERN_MARKER not in repr(sites)
    assert document.model_dump(mode="json") == original
    assert isinstance(result, CompletePatternCheck)
    assert [(error.path, error.offset) for error in result.diagnostics.errors] == [
        (sites[1].path, sources[0].index('"(?ii)')),
        (sites[2].path, sources[1].index('"(?ii)')),
    ]
    assert all(error.code == "IR011" for error in result.diagnostics.errors)
    assert_settled(owner)


def failure_document(*failures: dict[str, object]) -> GraphDocument:
    return document_for(
        nodes={"first": gate(predicate='"x".matches("^x$")')},
        bodies={
            "unused": {
                "nodes": {f"failure_{i}": item for i, item in enumerate(failures)}
            }
        },
    )


@contextmanager
def observe_checker_construction() -> Generator[list[str]]:
    constructions: list[str] = []
    previous = sys.getprofile()
    constructors = {
        ApprovalDefaultChecks.__init__.__code__,
        ApprovalPredicateChecks.__init__.__code__,
    }

    def observe(frame: FrameType, event: str, argument: object) -> None:
        if event == "call" and frame.f_code in constructors:
            constructions.append(frame.f_code.co_qualname)

    try:
        sys.setprofile(observe)
        yield constructions
    finally:
        sys.setprofile(previous)


def check_refusals() -> None:
    admitted, _ = inventory_document()
    typed = failure_document(gate(predicate="1"), gate({}))
    schema = failure_document(
        gate(predicate="1"), gate({"$ref": "https://example.invalid/private-schema"})
    )
    context = failure_document(
        gate({"$ref": "https://example.invalid/private-schema"}),
        gate(predicate="inputs.missing", inputs=unavailable_inputs()),
    )
    syntax = failure_document(
        gate(predicate="inputs.missing", inputs=unavailable_inputs()),
        gate(predicate="true &&"),
    )
    ordinary = document_for()
    owner = EvaluationThread(load_binding())
    cases = (
        (admitted, 14, WORK),
        (admitted, -1, WORK),
        (admitted, True, WORK),
        (admitted, 15, 1),
        (typed, 5, WORK),
        (schema, 5, WORK),
        (context, 5, WORK),
        (syntax, 5, WORK),
        (ordinary, 2, WORK),
    )

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            refused = tuple(
                loop.create_task(
                    validate_approval_patterns(
                        graph, evaluator, max_declarations=limit, max_work=work
                    )
                )
                for graph, limit, work in cases[:3]
            )
            with observe_checker_construction() as constructors:
                run_loop_turn(loop)
            tasks = tuple(
                loop.create_task(
                    validate_approval_patterns(
                        graph, evaluator, max_declarations=limit, max_work=work
                    )
                )
                for graph, limit, work in cases[3:]
            )
            run_loop_turn(loop)
            assert all(task.done() for task in tasks)
            results = tuple(task.result() for task in (*refused, *tasks))
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())

    assert constructors == []
    assert results[:4] == (
        IncompletePatternCheck.CAPACITY_EXHAUSTED,
        IncompletePatternCheck.INVALID_INVENTORY,
        IncompletePatternCheck.INVALID_INVENTORY,
        IncompletePatternCheck.CAPACITY_EXHAUSTED,
    )
    diagnostics = results[4]
    assert isinstance(diagnostics, DiagnosticCollection)
    assert len(diagnostics.errors) == 2
    assert diagnostics.errors_omitted == 0
    assert [error.path for error in diagnostics.errors] == [
        ("graph", "subgraphs", "unused", "nodes", name, "config", "approve_when")
        for name in ("failure_0", "failure_1")
    ]
    assert results[5:8] == (
        ApprovalPrerequisite.SCHEMA_UNRESOLVED,
        ApprovalPrerequisite.CONTEXT_UNRESOLVED,
        ApprovalPrerequisite.SYNTAX_UNRESOLVED,
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
            validate_approval_patterns(
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
            probe = runner.run(evaluator.validate_pattern(""))
            assert probe is PatternCompilation.ACCEPTED
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
        "approval-pattern-inventory-test",
        clock,
    )

    try:
        with bound_scope(request_id=REQUEST_ID):
            cases[scenario]()
    finally:
        closed = diagnostics.shutdown(5)

    assert closed
