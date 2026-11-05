import asyncio
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import FrameType
from typing import cast
from weakref import ref

import pytest

from kora.api.branch_renderings import (
    compile_branch_renderings,
    compile_branch_sites,
)
from kora.api.cel_renderings import (
    CompiledRenderings,
    IncompleteRenderings,
    RenderingRefusal,
)
from kora.api.exchange import COMPONENT
from kora.cel import CheckedExpression
from kora.cel.checked import SEAL
from kora.cel_eval.binding import load_binding
from kora.cel_eval.errors import EvaluatorUnavailable, Fault, UnavailableReason
from kora.cel_eval.thread import EvaluationThread, ValidationWork
from kora.ir.branch_expressions import (
    BranchExpressionResult,
    CheckedBranchSite,
    checked_branch_sites,
)
from kora.ir.expression_fields import FieldPath
from kora.observability import Level, LoggingSettings, bound_scope, configure_process
from kora.observability.settings import RenderFormat
from tests.api.branch_pattern_scenarios import (
    failure_document,
    inventory_document,
    observe_context_construction,
)
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
from tests.ir.approval_input_cases import unavailable_inputs
from tests.ir.branch_expression_cases import branch
from tests.ir.inline_schema_cases import document_for

CHARACTERS: int = 100_000


@dataclass
class Observations:
    paths: list[FieldPath] = field(default_factory=list[FieldPath])
    prepared: list[int] = field(default_factory=list[int])
    submitted: list[int] = field(default_factory=list[int])
    renderings: list[str] = field(default_factory=list[str])
    accepted: int = 0


@contextmanager
def observe_compilation() -> Generator[Observations]:
    counts = Observations()
    previous = sys.getprofile()

    def observe(frame: FrameType, event: str, argument: object) -> None:
        if frame.f_code is checked_branch_sites.__code__ and event == "return":
            if isinstance(argument, tuple):
                for value in cast(tuple[object, ...], argument):
                    assert isinstance(value, CheckedBranchSite)
                    counts.paths.append(value.path)
                    counts.prepared.append(id(value.checked))
                    counts.renderings.append(value.checked.rendering)
        elif frame.f_code is EvaluationThread.submit.__code__:
            if event == "call":
                work: object = frame.f_locals.get("work")
                assert isinstance(work, ValidationWork)
                counts.submitted.append(id(work.checked))
            elif event == "return" and isinstance(argument, asyncio.Future):
                counts.accepted += 1

    try:
        sys.setprofile(observe)
        yield counts
    finally:
        sys.setprofile(previous)


def check_inventory() -> None:
    document, _ = inventory_document()
    before = document.model_dump(mode="json")
    sites = checked_branch_sites(document, max_declarations=20)
    assert isinstance(sites, tuple)
    characters = sum(len(site.checked.rendering) for site in sites)
    del sites
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            with observe_compilation() as observed:
                result = observe_deliveries(
                    loop,
                    loop.create_task(
                        compile_branch_renderings(
                            document,
                            evaluator,
                            max_declarations=20,
                            max_rendering_characters=characters,
                        )
                    ),
                    owner,
                    7,
                )
            repeated = document_for(nodes={"first": branch(), "second": branch()})
            with observe_compilation() as duplicates:
                same_rendering = observe_deliveries(
                    loop,
                    loop.create_task(
                        compile_branch_renderings(
                            repeated,
                            evaluator,
                            max_declarations=10,
                            max_rendering_characters=CHARACTERS,
                        )
                    ),
                    owner,
                    2,
                )
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())

    assert result == CompiledRenderings(7)
    assert observed.paths == [
        ("graph", "nodes", "branch", "config", "cases", 0, "when"),
        ("graph", "nodes", "branch", "config", "cases", 1, "when"),
        ("graph", "nodes", "branch", "config", "cases", 1, "reason"),
        ("graph", "nodes", "branch", "config", "else_reason"),
        (
            "graph",
            "subgraphs",
            "unused",
            "nodes",
            "branch",
            "config",
            "cases",
            0,
            "when",
        ),
        (
            "graph",
            "subgraphs",
            "unused",
            "nodes",
            "branch",
            "config",
            "cases",
            0,
            "reason",
        ),
        (
            "graph",
            "subgraphs",
            "recursive",
            "nodes",
            "branch",
            "config",
            "cases",
            0,
            "when",
        ),
    ]
    assert observed.submitted == observed.prepared
    assert observed.accepted == 7
    assert same_rendering == CompiledRenderings(2)
    assert len(set(duplicates.renderings)) == 1
    assert duplicates.submitted == duplicates.prepared
    assert len(set(duplicates.prepared)) == 2
    assert duplicates.accepted == 2
    assert duplicates.paths == [
        ("graph", "nodes", name, "config", "cases", 0, "when")
        for name in ("first", "second")
    ]
    assert document.model_dump(mode="json") == before
    assert_settled(owner)


def check_refusals() -> None:
    admitted, _ = inventory_document()
    sites = checked_branch_sites(admitted, max_declarations=20)
    assert isinstance(sites, tuple)
    one_under = sum(len(site.checked.rendering) for site in sites) - 1
    del sites
    cases = (
        (admitted, 19, CHARACTERS),
        (admitted, -1, CHARACTERS),
        (admitted, True, CHARACTERS),
        (admitted, 20, -1),
        (admitted, 20, True),
        (admitted, 20, one_under),
        (failure_document(branch("1")), 6, CHARACTERS),
        (failure_document(branch("true &&")), 6, CHARACTERS),
        (
            failure_document(branch("inputs.missing", inputs=unavailable_inputs())),
            6,
            CHARACTERS,
        ),
        (failure_document(branch("params.ok"), schema=True), 6, CHARACTERS),
    )
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            with observe_compilation() as observed:
                refused = tuple(
                    loop.create_task(
                        compile_branch_renderings(
                            graph,
                            evaluator,
                            max_declarations=declarations,
                            max_rendering_characters=characters,
                        )
                    )
                    for graph, declarations, characters in cases[:5]
                )
                with observe_context_construction() as constructors:
                    run_loop_turn(loop)
                remaining = tuple(
                    loop.create_task(
                        compile_branch_renderings(
                            graph,
                            evaluator,
                            max_declarations=declarations,
                            max_rendering_characters=characters,
                        )
                    )
                    for graph, declarations, characters in cases[5:]
                )
                run_loop_turn(loop)
                assert all(task.done() for task in (*refused, *remaining))
                results = tuple(task.result() for task in (*refused, *remaining))
            empty = runner.run(
                compile_branch_renderings(
                    document_for(),
                    evaluator,
                    max_declarations=2,
                    max_rendering_characters=0,
                )
            )
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())

    assert constructors == []
    assert observed.accepted == 0
    assert observed.submitted == []
    assert results[:6] == tuple(
        IncompleteRenderings[FieldPath](reason)
        for reason in (
            RenderingRefusal.CAPACITY_EXHAUSTED,
            *([RenderingRefusal.INVALID_ALLOWANCE] * 4),
            RenderingRefusal.CAPACITY_EXHAUSTED,
        )
    )
    typed, syntax, context, schema = results[6:]
    assert isinstance(typed, BranchExpressionResult)
    assert typed.branch_expressions_complete
    assert len(typed.diagnostics.errors) == 1
    assert typed.diagnostics.errors[0].code == "IR011"
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
        not value.branch_expressions_valid for value in (typed, syntax, context, schema)
    )
    assert empty == CompiledRenderings(0)
    assert_settled(owner)


def malformed_sites() -> tuple[CheckedBranchSite, ...]:
    document, _ = inventory_document()
    sites = checked_branch_sites(document, max_declarations=20)
    assert isinstance(sites, tuple)
    original = sites[1]
    checked = original.checked
    malformed = CheckedExpression(
        SEAL,
        checked.tree,
        "(",
        checked.digest,
        checked.estimate,
        checked.bounds,
        checked.projections,
    )
    return (sites[0], CheckedBranchSite(original.path, malformed), *sites[2:])


def check_failures() -> None:
    document, _ = inventory_document()
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        baseline = checked_expression_count()
        try:
            with observe_compilation() as observed:
                failure = observe_deliveries(
                    loop,
                    loop.create_task(
                        compile_branch_sites(
                            malformed_sites(),
                            evaluator,
                            max_rendering_characters=CHARACTERS,
                        )
                    ),
                    owner,
                    2,
                )
            assert checked_expression_count() == baseline
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())
        with (
            pytest.raises(EvaluatorUnavailable) as unavailable,
            observe_compilation() as closed,
        ):
            runner.run(
                compile_branch_renderings(
                    document,
                    evaluator,
                    max_declarations=20,
                    max_rendering_characters=CHARACTERS,
                )
            )
        assert checked_expression_count() == baseline

    assert failure == IncompleteRenderings(
        RenderingRefusal.NATIVE_REFUSED,
        ("graph", "nodes", "branch", "config", "cases", 1, "when"),
        Fault.UNMAPPED,
    )
    assert observed.accepted == 2
    assert unavailable.value.reason is UnavailableReason.CLOSED
    assert closed.accepted == 0
    assert len(closed.submitted) == 1
    assert PATTERN_MARKER not in repr(failure)
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
            compile_branch_renderings(
                document,
                evaluator,
                max_declarations=20,
                max_rendering_characters=CHARACTERS,
            )
        )
        del document
        try:
            run_loop_turn(loop)
            delivery = wait_for_delivery(owner)
            assert document_ref() is None
            assert checked_expression_count() == baseline + 7
            assert task.cancel()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                loop.run_until_complete(task)
            assert cancelled.value.__context__ is None
            assert checked_expression_count() == baseline
            assert_idle(owner, 1)
            compiled = runner.run(
                compile_branch_renderings(
                    inventory_document()[0],
                    evaluator,
                    max_declarations=20,
                    max_rendering_characters=CHARACTERS,
                )
            )
            assert compiled == CompiledRenderings(7)
        finally:
            runner.run(evaluator.close())

    assert delivery.outstanding == delivery.deliveries == 1
    assert task.cancelled()
    assert_settled(owner)


def run(scenario: str) -> None:
    scenarios = {
        "inventory": check_inventory,
        "refusals": check_refusals,
        "failures": check_failures,
        "cancellation": check_cancellation,
    }
    diagnostics = configure_process(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG, render_format=RenderFormat.JSON),
        "branch-renderings-test",
        clock,
    )
    try:
        with bound_scope(request_id=REQUEST_ID):
            scenarios[scenario]()
    finally:
        closed = diagnostics.shutdown(5)
    assert closed
