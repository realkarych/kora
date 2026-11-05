import asyncio
import sys
from collections.abc import Coroutine, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import FrameType
from typing import Literal, cast
from weakref import ref

import pytest

from kora.api.cel_renderings import (
    CheckedRenderingSite,
    CompiledRenderings,
    IncompleteRenderings,
    RenderingRefusal,
)
from kora.api.exchange import COMPONENT
from kora.api.predicate_renderings import (
    RETRY_COMPLETED,
    SENSOR_COMPLETED,
    WAIT_COMPLETED,
    RenderingCheck,
    compile_predicate_inventory,
    compile_retry_renderings,
    compile_sensor_trigger_renderings,
    compile_wait_renderings,
)
from kora.cel import CheckedExpression, Environment
from kora.cel.checked import SEAL
from kora.cel_eval.binding import load_binding
from kora.cel_eval.errors import EvaluatorUnavailable, Fault, UnavailableReason
from kora.cel_eval.thread import EvaluationThread, Evaluator, ValidationWork
from kora.ir.document import GraphDocument
from kora.ir.expression_contexts import InputParameterContexts
from kora.ir.expression_fields import FieldPath
from kora.ir.retry_expressions import (
    CheckedRetrySite,
    RetryExpressionChecks,
    RetryExpressionResult,
    checked_retry_sites,
)
from kora.ir.trigger_expressions import (
    CheckedSensorTrigger,
    SensorTriggerExpressionResult,
    checked_sensor_trigger_sites,
)
from kora.ir.wait_expressions import (
    CheckedWaitSite,
    WaitExpressionChecks,
    WaitExpressionResult,
    checked_wait_sites,
)
from kora.observability import (
    EventSpec,
    Level,
    LoggingSettings,
    bound_scope,
    configure_process,
)
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
from tests.ir.approval_input_cases import BUILTINS, producer, unavailable_inputs
from tests.ir.branch_expression_cases import SCHEMA
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.retry_expression_cases import retrying
from tests.ir.trigger_expression_cases import sensor
from tests.ir.wait_expression_cases import wait

Family = Literal["wait", "retry", "sensor"]
Prerequisite = (
    WaitExpressionResult | RetryExpressionResult | SensorTriggerExpressionResult
)
Sites = tuple[CheckedRenderingSite[FieldPath], ...]
Outcome = RenderingCheck | Prerequisite
FAMILIES: tuple[Family, ...] = ("wait", "retry", "sensor")
LIMITS: dict[Family, int] = {"wait": 17, "retry": 15, "sensor": 4}
COUNTS: dict[Family, int] = {"wait": 5, "retry": 5, "sensor": 3}
EVENTS: dict[Family, EventSpec] = {
    "wait": WAIT_COMPLETED,
    "retry": RETRY_COMPLETED,
    "sensor": SENSOR_COMPLETED,
}
CHARACTERS: int = 100_000


def inventory_document(family: Family) -> GraphDocument:
    if family == "sensor":
        return document_for(
            triggers=[
                {"kind": "manual"},
                sensor(f'observation.ready && "😀".matches("^{PATTERN_MARKER}$")'),
                sensor("false"),
                sensor("false"),
            ]
        )
    inputs = {
        "data": {"type": "StructuredOutput", "from": "producer", "output": "value"}
    }
    if family == "wait":
        return document_for(
            contracts=BUILTINS,
            params=SCHEMA,
            nodes={
                "producer": producer(schema=SCHEMA),
                "event": wait(
                    "inputs.data.ok && params.ok && payload.ready", inputs=inputs
                ),
                "poll": wait("observation.ready", poll=True),
                "omitted": wait(None, inputs=unavailable_inputs()),
                "call": node("subgraph", config={"body": "recursive"}),
            },
            bodies={
                "unused": {
                    "nodes": {
                        "producer": producer(schema=SCHEMA),
                        "event": wait("payload.ready"),
                        "poll": wait(f'"😀".matches("^{PATTERN_MARKER}$")', poll=True),
                    }
                },
                "recursive": {
                    "nodes": {
                        "wait": wait(),
                        "again": node("subgraph", config={"body": "recursive"}),
                    }
                },
            },
        )
    return document_for(
        contracts=BUILTINS,
        params=SCHEMA,
        nodes={
            "producer": producer(schema=SCHEMA),
            "check": retrying(
                "has(outputs.value) && outputs.value.ok && inputs.data.ok && params.ok",
                producer(schema=SCHEMA),
                inputs=inputs,
            ),
            "omitted": retrying(None, inputs=unavailable_inputs()),
            "call": node("subgraph", config={"body": "recursive"}),
        },
        bodies={
            "unused": {
                "retry": {"max_attempts": 1, "predicate": "params.ok"},
                "nodes": {
                    "check": retrying(
                        "has(outputs.value) && outputs.value.ok",
                        producer(schema=SCHEMA),
                    ),
                    "timer": node(),
                },
            },
            "recursive": {
                "retry": {"max_attempts": 1, "predicate": "true"},
                "nodes": {
                    "check": retrying(f'"😀".matches("^{PATTERN_MARKER}$")'),
                    "again": node("subgraph", config={"body": "recursive"}),
                },
            },
        },
    )


def expected_paths(family: Family) -> list[FieldPath]:
    if family == "sensor":
        return [("graph", "triggers", index, "poll", "until") for index in (1, 2, 3)]
    if family == "wait":
        return [
            ("graph", "nodes", "event", "config", "event", "match"),
            ("graph", "nodes", "poll", "config", "poll", "until"),
            (
                "graph",
                "subgraphs",
                "unused",
                "nodes",
                "event",
                "config",
                "event",
                "match",
            ),
            (
                "graph",
                "subgraphs",
                "unused",
                "nodes",
                "poll",
                "config",
                "poll",
                "until",
            ),
            (
                "graph",
                "subgraphs",
                "recursive",
                "nodes",
                "wait",
                "config",
                "event",
                "match",
            ),
        ]
    return [
        ("graph", "nodes", "check", "retry", "predicate"),
        ("graph", "subgraphs", "unused", "retry", "predicate"),
        ("graph", "subgraphs", "unused", "nodes", "check", "retry", "predicate"),
        ("graph", "subgraphs", "recursive", "retry", "predicate"),
        ("graph", "subgraphs", "recursive", "nodes", "check", "retry", "predicate"),
    ]


def checked_inventory(family: Family, document: GraphDocument) -> Sites:
    if family == "sensor":
        inventory = checked_sensor_trigger_sites(document, max_triggers=LIMITS[family])
    elif family == "wait":
        inventory = checked_wait_sites(document, max_declarations=LIMITS[family])
    else:
        inventory = checked_retry_sites(document, max_declarations=LIMITS[family])
    assert isinstance(inventory, tuple)
    return inventory


def compile_family(
    family: Family,
    document: GraphDocument,
    evaluator: Evaluator,
    declarations: int,
    characters: int,
) -> Coroutine[None, None, Outcome]:
    if family == "sensor":
        return compile_sensor_trigger_renderings(
            document,
            evaluator,
            max_triggers=declarations,
            max_rendering_characters=characters,
        )
    if family == "wait":
        return compile_wait_renderings(
            document,
            evaluator,
            max_declarations=declarations,
            max_rendering_characters=characters,
        )
    return compile_retry_renderings(
        document,
        evaluator,
        max_declarations=declarations,
        max_rendering_characters=characters,
    )


@dataclass
class Observations:
    paths: list[FieldPath] = field(default_factory=list[FieldPath])
    prepared: list[int] = field(default_factory=list[int])
    submitted: list[int] = field(default_factory=list[int])
    renderings: list[str] = field(default_factory=list[str])
    prerequisites: list[Prerequisite] = field(default_factory=list[Prerequisite])
    accepted: int = 0
    contexts: int = 0


@contextmanager
def observe_compilation(family: Family) -> Generator[Observations]:
    counts = Observations()
    previous = sys.getprofile()
    producer_code = {
        "wait": checked_wait_sites.__code__,
        "retry": checked_retry_sites.__code__,
        "sensor": checked_sensor_trigger_sites.__code__,
    }[family]
    constructors = {
        WaitExpressionChecks.__init__.__code__,
        RetryExpressionChecks.__init__.__code__,
        InputParameterContexts.__init__.__code__,
        Environment.__init__.__code__,
    }

    def observe(frame: FrameType, event: str, argument: object) -> None:
        if frame.f_code in constructors and event == "call":
            counts.contexts += 1
        if frame.f_code is producer_code and event == "return":
            if isinstance(argument, tuple):
                for value in cast(tuple[object, ...], argument):
                    assert isinstance(
                        value, (CheckedWaitSite, CheckedRetrySite, CheckedSensorTrigger)
                    )
                    counts.paths.append(value.path)
                    counts.prepared.append(id(value.checked))
                    counts.renderings.append(value.checked.rendering)
            elif isinstance(
                argument,
                (
                    WaitExpressionResult,
                    RetryExpressionResult,
                    SensorTriggerExpressionResult,
                ),
            ):
                counts.prerequisites.append(argument)
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


def check_inventory(family: Family) -> None:
    document = inventory_document(family)
    before = document.model_dump(mode="json")
    sites = checked_inventory(family, document)
    characters = sum(len(site.checked.rendering) for site in sites)
    del sites
    if family == "sensor":
        duplicates = document_for(triggers=[sensor("true"), sensor("true")])
        paths: list[FieldPath] = [
            ("graph", "triggers", index, "poll", "until") for index in (0, 1)
        ]
    else:
        declare = wait if family == "wait" else retrying
        duplicates = document_for(
            nodes={name: declare("true") for name in ("first", "second")}
        )
        suffix = (
            ("config", "event", "match") if family == "wait" else ("retry", "predicate")
        )
        paths = [("graph", "nodes", name, *suffix) for name in ("first", "second")]
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            with observe_compilation(family) as observed:
                result = observe_deliveries(
                    loop,
                    loop.create_task(
                        compile_family(
                            family, document, evaluator, LIMITS[family], characters
                        )
                    ),
                    owner,
                    COUNTS[family],
                )
            with observe_compilation(family) as repeated:
                same = observe_deliveries(
                    loop,
                    loop.create_task(
                        compile_family(
                            family, duplicates, evaluator, LIMITS[family], CHARACTERS
                        )
                    ),
                    owner,
                    2,
                )
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())

    assert result == CompiledRenderings(COUNTS[family])
    assert observed.paths == expected_paths(family)
    assert observed.submitted == observed.prepared
    assert observed.accepted == COUNTS[family]
    assert same == CompiledRenderings(2)
    assert repeated.paths == paths
    assert len(set(repeated.renderings)) == 1
    assert repeated.submitted == repeated.prepared
    assert len(set(repeated.prepared)) == repeated.accepted == 2
    assert document.model_dump(mode="json") == before
    assert_settled(owner)


def failure_document(
    family: Family, text: str, *, context: bool = False, schema: bool = False
) -> GraphDocument:
    if family == "sensor":
        return document_for(triggers=[{"kind": "manual"}, sensor("true"), sensor(text)])
    declare = wait if family == "wait" else retrying
    return document_for(
        params={"$ref": "https://example.invalid/private-schema"} if schema else None,
        nodes={"first": declare("true")},
        bodies={
            "unused": {
                "nodes": {
                    "failure": declare(
                        text, inputs=unavailable_inputs() if context else {}
                    )
                }
            }
        },
    )


def check_refusals(family: Family) -> None:
    document = inventory_document(family)
    sites = checked_inventory(family, document)
    one_under = sum(len(site.checked.rendering) for site in sites) - 1
    del sites
    cases = (
        (LIMITS[family] - 1, CHARACTERS),
        (-1, CHARACTERS),
        (True, CHARACTERS),
        (cast(int, 1.5), CHARACTERS),
        (cast(int, "large"), CHARACTERS),
        (LIMITS[family], -1),
        (LIMITS[family], True),
        (LIMITS[family], cast(int, 1.5)),
        (LIMITS[family], cast(int, "large")),
        (LIMITS[family], one_under),
    )
    partials = [failure_document(family, "1"), failure_document(family, "true &&")]
    if family != "sensor":
        partials.extend(
            (
                failure_document(family, "inputs.missing", context=True),
                failure_document(family, "params.ok", schema=True),
            )
        )
    empty = document_for(triggers=[]) if family == "sensor" else document_for()
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        evaluator = runner.run(open_evaluator(owner))
        try:
            refused: list[Outcome] = []
            for index, (declarations, characters) in enumerate(cases):
                with observe_compilation(family) as observed:
                    result = runner.run(
                        compile_family(
                            family, document, evaluator, declarations, characters
                        )
                    )
                assert observed.submitted == []
                if index < len(cases) - 1:
                    assert observed.contexts == 0
                refused.append(result)
            for partial in partials:
                with observe_compilation(family) as observed:
                    result = runner.run(
                        compile_family(
                            family, partial, evaluator, LIMITS[family], CHARACTERS
                        )
                    )
                assert len(observed.prerequisites) == 1
                assert result is observed.prerequisites[0]
                assert observed.submitted == []
            with observe_compilation(family) as observed:
                no_sites = runner.run(
                    compile_family(
                        family, empty, evaluator, 0 if family == "sensor" else 2, 0
                    )
                )
            assert observed.submitted == []
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())

    assert refused == [
        IncompleteRenderings[FieldPath](RenderingRefusal.CAPACITY_EXHAUSTED),
        *[
            IncompleteRenderings[FieldPath](RenderingRefusal.INVALID_ALLOWANCE)
            for _ in range(8)
        ],
        IncompleteRenderings[FieldPath](RenderingRefusal.CAPACITY_EXHAUSTED),
    ]
    assert no_sites == CompiledRenderings(0)
    assert_settled(owner)


def malformed_inventory(family: Family) -> Sites:
    sites = checked_inventory(family, inventory_document(family))
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
    if isinstance(original, CheckedWaitSite):
        replacement = CheckedWaitSite(original.path, malformed)
    elif isinstance(original, CheckedRetrySite):
        replacement = CheckedRetrySite(original.path, malformed)
    else:
        assert isinstance(original, CheckedSensorTrigger)
        replacement = CheckedSensorTrigger(original.path, malformed)
    return (sites[0], replacement, *sites[2:])


def check_failures(family: Family) -> None:
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        baseline = checked_expression_count()
        try:
            with observe_compilation(family) as observed:
                verification: Coroutine[
                    None, None, RenderingCheck | WaitExpressionResult
                ] = compile_predicate_inventory(
                    malformed_inventory(family),
                    evaluator,
                    max_rendering_characters=CHARACTERS,
                    event=EVENTS[family],
                )
                failure = observe_deliveries(
                    loop, loop.create_task(verification), owner, 2
                )
            assert checked_expression_count() == baseline
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())
        document = inventory_document(family)
        document_ref = ref(document)
        operation = compile_family(
            family, document, evaluator, LIMITS[family], CHARACTERS
        )
        del document
        with (
            pytest.raises(EvaluatorUnavailable) as unavailable,
            observe_compilation(family) as closed,
        ):
            runner.run(operation)
        assert checked_expression_count() == baseline
        assert document_ref() is None

    assert failure == IncompleteRenderings(
        RenderingRefusal.NATIVE_REFUSED, expected_paths(family)[1], Fault.UNMAPPED
    )
    assert observed.accepted == len(observed.submitted) == 2
    assert unavailable.value.reason is UnavailableReason.CLOSED
    assert closed.accepted == 0
    assert len(closed.submitted) == 1
    assert PATTERN_MARKER not in repr(failure)
    assert_settled(owner)


def check_cancellation(family: Family) -> None:
    document = inventory_document(family)
    document_ref = ref(document)
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        baseline = checked_expression_count()
        task = loop.create_task(
            compile_family(family, document, evaluator, LIMITS[family], CHARACTERS)
        )
        del document
        try:
            run_loop_turn(loop)
            delivery = wait_for_delivery(owner)
            assert document_ref() is None
            assert checked_expression_count() == baseline + COUNTS[family]
            assert task.cancel()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                loop.run_until_complete(task)
            assert cancelled.value.__context__ is None
            assert checked_expression_count() == baseline
            assert_idle(owner, 1)
            reused = runner.run(
                compile_family(
                    family,
                    inventory_document(family),
                    evaluator,
                    LIMITS[family],
                    CHARACTERS,
                )
            )
            assert reused == CompiledRenderings(COUNTS[family])
        finally:
            runner.run(evaluator.close())

    assert delivery.outstanding == delivery.deliveries == 1
    assert task.cancelled()
    assert_settled(owner)


def run(family: Family, scenario: str) -> None:
    cases = {
        "inventory": check_inventory,
        "refusals": check_refusals,
        "failures": check_failures,
        "cancellation": check_cancellation,
    }
    diagnostics = configure_process(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG, render_format=RenderFormat.JSON),
        "predicate-renderings-test",
        clock,
    )

    try:
        with bound_scope(request_id=REQUEST_ID):
            cases[scenario](family)
    finally:
        closed = diagnostics.shutdown(5)

    assert closed
