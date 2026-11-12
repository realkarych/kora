import asyncio
import gc
import json
import sys
from collections.abc import Coroutine, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from functools import partial
from types import CodeType, FrameType
from typing import cast
from weakref import ref

import pytest
from fastapi.encoders import jsonable_encoder

from kora.api.cel_pattern_validation import (
    CheckedPatternSite,
    IncompletePatternCheck,
    PreparedPatterns,
)
from kora.api.cel_renderings import IncompleteRenderings, RenderingRefusal
from kora.api.exchange import COMPONENT
from kora.api.graph_cel import (
    GraphCelChecks,
    GraphCelRefusal,
    GraphCelResult,
    verify_graph_cel,
)
from kora.cel import Environment
from kora.cel_eval.binding import Binding, NativeCompile, NativeProgram, load_binding
from kora.cel_eval.errors import EvaluatorUnavailable, Fault, UnavailableReason
from kora.cel_eval.thread import EvaluationThread, Evaluator
from kora.db.graph_value_json import ObjectReader
from kora.engine.graph_cel_facts import GraphCelFacts
from kora.ir.approval_compilation import ApprovalPrerequisite, checked_approval_sites
from kora.ir.approval_defaults import ApprovalDefaultChecks
from kora.ir.approval_predicates import ApprovalPredicateChecks
from kora.ir.branch_expressions import (
    BranchExpressionChecks,
    BranchExpressionResult,
    checked_branch_sites,
)
from kora.ir.cel_inventory_admission import CheckedInventoryAdmission
from kora.ir.diagnostic_collection import DiagnosticCollection
from kora.ir.document import GraphDocument
from kora.ir.expression_contexts import InputParameterContexts
from kora.ir.expression_fields import FieldPath
from kora.ir.graph_cel_inventory import checked_graph_sites
from kora.ir.json import JsonObject
from kora.ir.retry_expressions import (
    CheckedRetrySite,
    InfrastructureRetryPrerequisite,
    RetryExpressionChecks,
    RetryExpressionResult,
    checked_infrastructure_retry_site,
    checked_retry_sites,
)
from kora.ir.trigger_expressions import (
    SensorTriggerExpressionResult,
    checked_sensor_trigger_sites,
)
from kora.ir.wait_expressions import (
    WaitExpressionChecks,
    WaitExpressionResult,
    checked_wait_sites,
)
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
PREEXISTING_RENDERING_CHARACTERS: int = 360
PREEXISTING_PATTERN_WORK: int = 600
SCOPED_TYPED_WORK: int = 600
SCOPED_TYPED_BYTES: int = 3500
INFRASTRUCTURE_PATH: FieldPath = ("runtime", "infrastructure_retry", "predicate")
PARAMETER_SCHEMA: JsonObject = {
    "$ref": "#/$defs/flags",
    "$defs": {
        "flags": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        }
    },
}
UNRESOLVED_SCHEMA: JsonObject = {"$ref": "#/$defs/missing"}
PRIVATE_SCHEMA: JsonObject = {"$ref": "https://example.invalid/private-schema"}
PATTERN_OFFSET: int = len('"a".matches(')
APPROVAL_PATTERN_OFFSET: int = len('true || "😀".matches(')
REFUSAL_MARKER: str = "private-native-refusal-marker"
REFUSED_REASON: str = f'"{REFUSAL_MARKER}"'
REFUSED_PATH: FieldPath = ("graph", "nodes", "branch", "config", "cases", 0, "reason")
REFUSED_DELIVERIES: int = 2
CHECKER_CONSTRUCTORS: frozenset[CodeType] = frozenset(
    {
        BranchExpressionChecks.__init__.__code__,
        ApprovalDefaultChecks.__init__.__code__,
        ApprovalPredicateChecks.__init__.__code__,
        RetryExpressionChecks.__init__.__code__,
        WaitExpressionChecks.__init__.__code__,
        InputParameterContexts.__init__.__code__,
        Environment.__init__.__code__,
    }
)
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
    *,
    rejected: bool = False,
    wait_node: dict[str, object] | None = None,
    params: JsonObject | None = None,
    reason: str = '"reason"',
) -> GraphDocument:
    prefix = "(?ii)" if rejected else "^"
    escaped = r"\u0070" + PATTERN_MARKER[1:]
    predicate = f'"a".matches("{prefix}{PATTERN_MARKER}")'
    escaped_predicate = f'true || "😀".matches("{prefix}{escaped}")'
    return document_for(
        contracts=BUILTINS,
        params=params,
        nodes={
            "branch": branch(predicate, reason, '"fallback"'),
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


@dataclass(frozen=True)
class RefusingCompile:
    native: NativeCompile
    renderings: list[str] = field(default_factory=list[str])

    def __call__(self, expression: str) -> NativeProgram:
        self.renderings.append(expression)
        if REFUSAL_MARKER in expression:
            raise ValueError(expression)
        return self.native(expression)


@contextmanager
def observe_checker_construction() -> Generator[list[str]]:
    constructions: list[str] = []
    previous = sys.getprofile()

    def observe(frame: FrameType, event: str, argument: object) -> None:
        if event == "call" and frame.f_code in CHECKER_CONSTRUCTORS:
            constructions.append(frame.f_code.co_qualname)

    try:
        sys.setprofile(observe)
        yield constructions
    finally:
        sys.setprofile(previous)


@contextmanager
def observe_rendering_boundary() -> Generator[tuple[list[str], list[object]]]:
    checkers: list[str] = []
    submitted: list[object] = []
    previous = sys.getprofile()

    def observe(frame: FrameType, event: str, argument: object) -> None:
        if event == "call" and frame.f_code in CHECKER_CONSTRUCTORS:
            checkers.append(frame.f_code.co_qualname)
        elif event == "call" and frame.f_code is EvaluationThread.submit.__code__:
            submitted.append(frame.f_locals.get("work"))

    try:
        sys.setprofile(observe)
        yield checkers, submitted
    finally:
        sys.setprofile(previous)


def with_infrastructure(
    document: GraphDocument, predicate: str | None
) -> GraphDocument:
    raw = document.model_dump(mode="json")
    policy: dict[str, object] = {"max_attempts": 1}
    if predicate is not None:
        policy["predicate"] = predicate
    raw["runtime"] = {"runtime": "local", "vcs": "git", "infrastructure_retry": policy}
    return GraphDocument.model_validate(raw)


def verify(
    document: GraphDocument,
    evaluator: Evaluator,
    *,
    declarations: int = DECLARATIONS,
    triggers: int = 2,
    characters: int = WORK,
    pattern_work: int = WORK,
    facts_bytes: int | None = None,
    typed_work: int | None = None,
    typed_bytes: int | None = None,
) -> Coroutine[None, None, GraphCelResult]:
    return verify_graph_cel(
        document,
        evaluator,
        max_declarations=declarations,
        max_triggers=triggers,
        max_rendering_characters=characters,
        max_pattern_work=pattern_work,
        max_facts_bytes=facts_bytes,
        max_typed_work=typed_work,
        max_typed_bytes=typed_bytes,
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
    assert valid.facts is None
    assert isinstance(invalid, GraphCelChecks)
    assert invalid.compiled_fields == FIELDS
    assert not invalid.diagnostics.valid
    assert len(invalid.diagnostics.errors) == len(REJECTED_OFFSETS)
    assert {item.path: item.offset for item in invalid.diagnostics.errors} == (
        REJECTED_OFFSETS
    )
    assert {item.code for item in invalid.diagnostics.errors} == {"IR011"}
    assert_settled(owner)


def stored_failure(
    root: dict[str, object],
    failure: dict[str, object],
    *,
    schema: bool = False,
) -> GraphDocument:
    return document_for(
        params=PRIVATE_SCHEMA if schema else None,
        nodes={"first": root},
        bodies={"unused": {"nodes": {"failure": failure}}},
    )


def approval_failures(*failures: dict[str, object]) -> GraphDocument:
    return document_for(
        nodes={"first": gate(predicate="true")},
        bodies={
            "unused": {
                "nodes": {
                    f"failure_{index}": failure
                    for index, failure in enumerate(failures)
                }
            }
        },
    )


def assert_branch_static(
    typed: GraphCelResult,
    syntax: GraphCelResult,
    context: GraphCelResult,
    schema: GraphCelResult,
) -> None:
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
    assert not typed.branch_expressions_valid
    assert not syntax.branch_expressions_valid
    assert not context.branch_expressions_valid
    assert not schema.branch_expressions_valid


def assert_wait_static(
    typed: GraphCelResult,
    syntax: GraphCelResult,
    context: GraphCelResult,
    schema: GraphCelResult,
) -> None:
    assert isinstance(typed, WaitExpressionResult)
    assert typed.wait_expressions_complete
    assert len(typed.diagnostics.errors) == 1
    assert typed.diagnostics.errors[0].path == (
        "graph",
        "subgraphs",
        "unused",
        "nodes",
        "failure",
        "config",
        "event",
        "match",
    )
    assert isinstance(syntax, WaitExpressionResult)
    assert syntax.syntax_unresolved == 1
    assert isinstance(context, WaitExpressionResult)
    assert context.context_unresolved == 1
    assert isinstance(schema, WaitExpressionResult)
    assert schema.schema_unresolved == 1
    assert not typed.wait_expressions_valid
    assert not syntax.wait_expressions_valid
    assert not context.wait_expressions_valid
    assert not schema.wait_expressions_valid


def assert_retry_static(
    typed: GraphCelResult,
    syntax: GraphCelResult,
    context: GraphCelResult,
    schema: GraphCelResult,
    body: GraphCelResult,
) -> None:
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
    assert not typed.retry_expressions_valid
    assert not syntax.retry_expressions_valid
    assert not context.retry_expressions_valid
    assert not schema.retry_expressions_valid
    assert isinstance(body, RetryExpressionResult)
    assert body.context_unresolved == 1
    assert not body.retry_expressions_complete


def assert_approval_static(
    schema: GraphCelResult,
    context: GraphCelResult,
    typed: GraphCelResult,
) -> None:
    assert schema is ApprovalPrerequisite.SCHEMA_UNRESOLVED
    assert context is ApprovalPrerequisite.CONTEXT_UNRESOLVED
    assert isinstance(typed, DiagnosticCollection)
    assert typed.errors_omitted == 0
    assert [(error.code, error.path) for error in typed.errors] == [
        (
            "IR011",
            (
                "graph",
                "subgraphs",
                "unused",
                "nodes",
                name,
                "config",
                "approve_when",
            ),
        )
        for name in ("failure_0", "failure_1")
    ]


def assert_sensor_static(typed: GraphCelResult, syntax: GraphCelResult) -> None:
    assert isinstance(typed, SensorTriggerExpressionResult)
    assert len(typed.diagnostics.errors) == 1
    assert typed.diagnostics.errors[0].path == (
        "graph",
        "triggers",
        1,
        "poll",
        "until",
    )
    assert typed.expressions_unresolved == 0
    assert not typed.expressions_valid
    assert isinstance(syntax, SensorTriggerExpressionResult)
    assert syntax.expressions_unresolved == 1
    assert not syntax.diagnostics.errors
    assert not syntax.expressions_valid


def check_refusals() -> None:
    document = mixed_document()
    single_family = branch_document(
        f'"a".matches("^{PATTERN_MARKER}")', '"reason"', '"fallback"'
    )
    invalid_wait = mixed_document(wait_node=wait("unknown", poll=True))
    missing_input = mixed_document(
        wait_node=wait("inputs.unavailable.ok", poll=True, inputs=unavailable_inputs())
    )
    approval_syntax = document_for(nodes={"gate": gate(predicate="true &&")})
    valid = '"x".matches("^x$")'
    branch_typed_document = stored_failure(branch(valid), branch("1"))
    branch_syntax_document = stored_failure(branch(valid), branch("true &&"))
    branch_context_document = stored_failure(
        branch(valid), branch("inputs.missing", inputs=unavailable_inputs())
    )
    branch_schema_document = stored_failure(
        branch(valid), branch("params.ok"), schema=True
    )
    wait_typed_document = stored_failure(wait(valid), wait("1"))
    wait_syntax_document = stored_failure(wait(valid), wait("true &&"))
    wait_context_document = stored_failure(
        wait(valid), wait("inputs.missing", inputs=unavailable_inputs())
    )
    wait_schema_document = stored_failure(wait(valid), wait("params.ok"), schema=True)
    retry_typed_document = stored_failure(retrying(valid), retrying("1"))
    retry_syntax_document = stored_failure(retrying(valid), retrying("true &&"))
    retry_context_document = stored_failure(
        retrying(valid), retrying("inputs.missing", inputs=unavailable_inputs())
    )
    retry_schema_document = stored_failure(
        retrying(valid), retrying("params.ok"), schema=True
    )
    retry_body_document = document_for(
        nodes={"first": retrying(valid)},
        bodies={
            "unused": {
                "retry": {"max_attempts": 1, "predicate": "has(outputs.value)"},
                "nodes": {"timer": node()},
            }
        },
    )
    approval_schema_document = approval_failures(
        gate(predicate="1"), gate(PRIVATE_SCHEMA)
    )
    approval_context_document = approval_failures(
        gate(PRIVATE_SCHEMA),
        gate(predicate="inputs.missing", inputs=unavailable_inputs()),
    )
    approval_typed_document = approval_failures(gate(predicate="1"), gate({}))
    sensor_typed_document = document_for(triggers=[sensor("true"), sensor("1")])
    sensor_syntax_document = document_for(triggers=[sensor("true"), sensor("true &&")])
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
            rendering_boolean = cast(int, True)
            with observe_rendering_boundary() as (rendering_checkers, submitted):
                negative_characters = runner.run(
                    verify(
                        document,
                        evaluator,
                        declarations=DECLARATIONS,
                        triggers=2,
                        characters=-1,
                        pattern_work=WORK,
                    )
                )
                boolean_characters = runner.run(
                    verify(
                        document,
                        evaluator,
                        declarations=DECLARATIONS,
                        triggers=2,
                        characters=rendering_boolean,
                        pattern_work=WORK,
                    )
                )
        finally:
            runner.run(evaluator.close())
        with observe_checker_construction() as admitted_checkers:
            admitted = checked_graph_sites(document, DECLARATIONS, 2)
        with observe_checker_construction() as refused_checkers:
            invalid = runner.run(verify(document, evaluator, pattern_work=0))
            boolean = runner.run(verify(document, evaluator, declarations=True))
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
        typed_refusal = runner.run(verify(invalid_wait, evaluator))
        unresolved = runner.run(verify(missing_input, evaluator))
        approval_unresolved = runner.run(verify(approval_syntax, evaluator))
        branch_typed = runner.run(verify(branch_typed_document, evaluator))
        branch_syntax = runner.run(verify(branch_syntax_document, evaluator))
        branch_context = runner.run(verify(branch_context_document, evaluator))
        branch_schema = runner.run(verify(branch_schema_document, evaluator))
        wait_typed = runner.run(verify(wait_typed_document, evaluator))
        wait_syntax = runner.run(verify(wait_syntax_document, evaluator))
        wait_context = runner.run(verify(wait_context_document, evaluator))
        wait_schema = runner.run(verify(wait_schema_document, evaluator))
        retry_typed = runner.run(verify(retry_typed_document, evaluator))
        retry_syntax = runner.run(verify(retry_syntax_document, evaluator))
        retry_context = runner.run(verify(retry_context_document, evaluator))
        retry_schema = runner.run(verify(retry_schema_document, evaluator))
        retry_body = runner.run(verify(retry_body_document, evaluator))
        approval_schema = runner.run(verify(approval_schema_document, evaluator))
        approval_context = runner.run(verify(approval_context_document, evaluator))
        approval_typed = runner.run(verify(approval_typed_document, evaluator))
        sensor_typed = runner.run(verify(sensor_typed_document, evaluator))
        sensor_syntax = runner.run(verify(sensor_syntax_document, evaluator))

    assert isinstance(prefix, GraphCelChecks)
    assert prefix.compiled_fields == 3
    assert prefix.diagnostics.valid
    assert isinstance(admitted, tuple)
    assert len(admitted) == FIELDS
    assert set(admitted_checkers) == {
        constructor.co_qualname for constructor in CHECKER_CONSTRUCTORS
    }
    assert refused_checkers == []
    assert rendering_checkers == []
    assert submitted == []
    assert (
        invalid
        is boolean
        is negative_characters
        is boolean_characters
        is GraphCelRefusal.INVALID_ALLOWANCE
    )
    assert oversized_graph is oversized_triggers is GraphCelRefusal.CAPACITY_EXHAUSTED
    assert oversized_patterns is IncompletePatternCheck.CAPACITY_EXHAUSTED
    assert oversized_renderings == IncompleteRenderings(
        RenderingRefusal.CAPACITY_EXHAUSTED
    )
    assert isinstance(typed_refusal, WaitExpressionResult)
    assert not typed_refusal.wait_expressions_valid
    assert isinstance(unresolved, WaitExpressionResult)
    assert unresolved.context_unresolved == 1
    assert unresolved.diagnostics.valid
    assert not unresolved.wait_expressions_complete
    assert approval_unresolved is ApprovalPrerequisite.SYNTAX_UNRESOLVED
    assert_branch_static(branch_typed, branch_syntax, branch_context, branch_schema)
    assert_wait_static(wait_typed, wait_syntax, wait_context, wait_schema)
    assert_retry_static(
        retry_typed, retry_syntax, retry_context, retry_schema, retry_body
    )
    assert_approval_static(approval_schema, approval_context, approval_typed)
    assert_sensor_static(sensor_typed, sensor_syntax)
    assert_settled(owner)


def check_infrastructure() -> None:
    base = mixed_document(rejected=True, params=PARAMETER_SCHEMA)
    source = f'params.enabled && "a".matches("(?ii){PATTERN_MARKER}")'
    with_predicate = with_infrastructure(base, source)
    admitted = (
        with_infrastructure(document_for(), None),
        with_infrastructure(document_for(params=UNRESOLVED_SCHEMA), "true"),
        with_infrastructure(document_for(params={}), "params.enabled"),
    )
    invalid = tuple(
        (
            with_infrastructure(document_for(params=schema), expression),
            offset,
        )
        for schema, expression, offset in (
            (None, "params.enabled", 7),
            (PARAMETER_SCHEMA, "[true].exists(params, params)", 7),
            (PARAMETER_SCHEMA, "inputs.ready", 0),
            (PARAMETER_SCHEMA, "outputs.ready", 0),
            (PARAMETER_SCHEMA, "failure.message", 0),
            (PARAMETER_SCHEMA, "1", 0),
        )
    )
    missing_schema = with_infrastructure(
        document_for(params=UNRESOLVED_SCHEMA), "params.enabled"
    )
    bad_syntax = with_infrastructure(document_for(), "(")
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            results = [runner.run(verify(document, evaluator)) for document in admitted]
            prefix = runner.run(
                verify(
                    base,
                    evaluator,
                    characters=PREEXISTING_RENDERING_CHARACTERS,
                    pattern_work=PREEXISTING_PATTERN_WORK,
                )
            )
            native = observe_deliveries(
                loop,
                loop.create_task(verify(with_predicate, evaluator)),
                owner,
                FIELDS + 2,
            )
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())
        static = [
            (runner.run(verify(document, evaluator)), offset)
            for document, offset in invalid
        ]
        schema_refusal = runner.run(verify(missing_schema, evaluator))
        syntax_refusal = runner.run(verify(bad_syntax, evaluator))
        rendering_refusal = runner.run(
            verify(
                with_predicate, evaluator, characters=PREEXISTING_RENDERING_CHARACTERS
            )
        )
        pattern_refusal = runner.run(
            verify(with_predicate, evaluator, pattern_work=PREEXISTING_PATTERN_WORK)
        )

    for result, count in zip(results, (0, 1, 1), strict=True):
        assert isinstance(result, GraphCelChecks)
        assert result.compiled_fields == count
        assert result.diagnostics.valid
    assert isinstance(prefix, GraphCelChecks)
    assert prefix.compiled_fields == FIELDS
    assert isinstance(native, GraphCelChecks)
    assert native.compiled_fields == FIELDS + 1
    assert len(native.diagnostics.errors) == len(REJECTED_OFFSETS) + 1
    assert {item.path: item.offset for item in native.diagnostics.errors} == {
        **REJECTED_OFFSETS,
        INFRASTRUCTURE_PATH: source.index('"(?ii)'),
    }
    assert {item.code for item in native.diagnostics.errors} == {"IR011"}
    for refusal, offset in static:
        assert isinstance(refusal, DiagnosticCollection)
        assert [(item.code, item.path, item.offset) for item in refusal.errors] == [
            ("IR011", INFRASTRUCTURE_PATH, offset)
        ]
    assert schema_refusal is InfrastructureRetryPrerequisite.SCHEMA_UNRESOLVED
    assert syntax_refusal is InfrastructureRetryPrerequisite.SYNTAX_UNRESOLVED
    assert rendering_refusal == IncompleteRenderings(
        RenderingRefusal.CAPACITY_EXHAUSTED
    )
    assert pattern_refusal is IncompletePatternCheck.CAPACITY_EXHAUSTED
    assert_settled(owner)


def pending_patterns() -> int:
    gc.collect()
    return sum(
        isinstance(value, PreparedPatterns) and bool(value.fields or value.probes)
        for value in gc.get_objects()
    )


def pending_facts() -> int:
    gc.collect()
    return sum(isinstance(value, GraphCelFacts) for value in gc.get_objects())


def check_typed_limits() -> None:
    source = f'params.enabled && "a".matches("^{PATTERN_MARKER}")'
    document = with_infrastructure(mixed_document(params=PARAMETER_SCHEMA), source)
    small = with_infrastructure(document_for(params=PARAMETER_SCHEMA), "params.enabled")
    wide_schema: JsonObject = {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            **{f"flag{index}": {"type": "boolean"} for index in range(200)},
        },
        "required": ["enabled"],
    }
    wide = with_infrastructure(document_for(params=wide_schema), "params.enabled")
    scoped = (
        partial(checked_branch_sites, document, max_declarations=DECLARATIONS),
        partial(checked_approval_sites, document, max_declarations=DECLARATIONS),
        partial(checked_retry_sites, document, max_declarations=DECLARATIONS),
        partial(checked_wait_sites, document, max_declarations=DECLARATIONS),
        partial(checked_sensor_trigger_sites, document, max_triggers=2),
        partial(checked_infrastructure_retry_site, document),
    )
    owner = EvaluationThread(load_binding())

    scoped_counts: list[int] = []
    scoped_work: list[int] = []
    scoped_bytes: list[int] = []
    for check in scoped:
        allowance = CheckedInventoryAdmission(SCOPED_TYPED_WORK, SCOPED_TYPED_BYTES)
        result = check(admission=allowance)
        assert isinstance(result, (tuple, CheckedRetrySite))
        scoped_counts.append(len(result) if isinstance(result, tuple) else 1)
        scoped_work.append(SCOPED_TYPED_WORK - allowance.remaining_work)
        scoped_bytes.append(SCOPED_TYPED_BYTES - allowance.remaining_bytes)
    result = None
    whole_work = sum(scoped_work)
    whole_bytes = sum(scoped_bytes)
    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            full = observe_deliveries(
                loop,
                loop.create_task(
                    verify(
                        document,
                        evaluator,
                        facts_bytes=WORK,
                        typed_work=whole_work,
                        typed_bytes=whole_bytes,
                    )
                ),
                owner,
                FIELDS + 2,
            )
            small_result = runner.run(
                verify(
                    small,
                    evaluator,
                    typed_work=SCOPED_TYPED_WORK,
                    typed_bytes=SCOPED_TYPED_BYTES,
                )
            )
            wide_result = runner.run(
                verify(wide, evaluator, typed_work=WORK, typed_bytes=SCOPED_TYPED_BYTES)
            )
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())
        baseline = checked_expression_count()
        retained_facts = pending_facts()
        work_refused = runner.run(
            verify(
                document,
                evaluator,
                facts_bytes=WORK,
                typed_work=whole_work - 1,
                typed_bytes=whole_bytes,
            )
        )
        bytes_refused = runner.run(
            verify(
                document,
                evaluator,
                facts_bytes=WORK,
                typed_work=whole_work,
                typed_bytes=whole_bytes - 1,
            )
        )
        schema_refused = runner.run(
            verify(
                wide,
                evaluator,
                typed_work=SCOPED_TYPED_WORK,
                typed_bytes=SCOPED_TYPED_BYTES,
            )
        )
        partial_limit = runner.run(verify(document, evaluator, typed_bytes=WORK))
        invalid_limit = runner.run(
            verify(document, evaluator, typed_work=WORK, typed_bytes=0)
        )

    assert scoped_counts == [3, 2, 2, 1, 1, 1]
    assert all(work > 0 for work in scoped_work)
    assert all(size > 0 for size in scoped_bytes)
    assert isinstance(full, GraphCelChecks)
    assert full.compiled_fields == FIELDS + 1
    assert full.diagnostics.valid
    assert full.facts is not None
    for admitted in (small_result, wide_result):
        assert isinstance(admitted, GraphCelChecks)
        assert admitted.compiled_fields == 1
        assert admitted.diagnostics.valid
    assert (
        work_refused
        is bytes_refused
        is schema_refused
        is GraphCelRefusal.CAPACITY_EXHAUSTED
    )
    assert partial_limit is invalid_limit is GraphCelRefusal.INVALID_ALLOWANCE
    assert checked_expression_count() == baseline
    assert pending_facts() == retained_facts
    assert_settled(owner)


def check_facts() -> None:
    source = f'params.enabled && "a".matches("^{PATTERN_MARKER}")'
    document = with_infrastructure(mixed_document(params=PARAMETER_SCHEMA), source)
    rejected = with_infrastructure(
        mixed_document(rejected=True, params=PARAMETER_SCHEMA),
        source.replace("^", "(?ii)"),
    )
    single_site = document_for(
        params=PARAMETER_SCHEMA,
        contracts=BUILTINS,
        nodes={
            "gate": gate(predicate='response.decision == "approve" && params.enabled')
        },
    )
    expected = checked_graph_sites(document, DECLARATIONS, 2)
    owner = EvaluationThread(load_binding())

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            captured = observe_deliveries(
                loop,
                loop.create_task(verify(document, evaluator, facts_bytes=WORK)),
                owner,
                FIELDS + 2,
            )
            invalid = observe_deliveries(
                loop,
                loop.create_task(verify(rejected, evaluator, facts_bytes=WORK)),
                owner,
                FIELDS + 2,
            )
            prefix = runner.run(verify(single_site, evaluator, facts_bytes=512))
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())
        refused = runner.run(verify(document, evaluator, facts_bytes=512))
        invalid_budget = runner.run(verify(document, evaluator, facts_bytes=0))
        empty = runner.run(verify(document_for(), evaluator, facts_bytes=512))

    assert isinstance(captured, GraphCelChecks)
    assert captured.facts is not None
    assert captured.diagnostics.valid
    assert captured.compiled_fields == FIELDS + 1
    assert PATTERN_MARKER not in repr(captured)
    with pytest.raises(TypeError, match="graph CEL facts require explicit access"):
        asdict(captured)
    with pytest.raises(TypeError, match="graph CEL facts require explicit access"):
        jsonable_encoder(captured)
    assert isinstance(invalid, GraphCelChecks)
    assert not invalid.diagnostics.valid
    assert invalid.facts is None
    assert isinstance(prefix, GraphCelChecks)
    assert prefix.compiled_fields == 1
    assert prefix.facts is not None
    prefix_fields = cast(
        list[JsonObject], ObjectReader(prefix.facts.text).object()["fields"]
    )
    prefix_reads = cast(JsonObject, prefix_fields[0]["reads"])
    assert list(prefix_reads) == ["params", "response"]
    assert prefix_reads == {
        "params": {"whole": [".enabled"], "presence": []},
        "response": {"whole": [".decision"], "presence": []},
    }
    assert refused is GraphCelRefusal.CAPACITY_EXHAUSTED
    assert invalid_budget is GraphCelRefusal.INVALID_ALLOWANCE
    assert isinstance(empty, GraphCelChecks)
    assert empty.facts is not None
    assert (
        empty.facts.text
        == '{"interpretation":"kora.cel-expression-facts.v1","fields":[]}'
    )
    assert isinstance(expected, tuple)
    verify_captured_facts(captured.facts, expected)
    assert_settled(owner)


def verify_captured_facts(
    facts: GraphCelFacts, expected: tuple[CheckedPatternSite, ...]
) -> None:
    saved = ObjectReader(facts.text).object()
    assert list(saved) == ["interpretation", "fields"]
    assert saved["interpretation"] == "kora.cel-expression-facts.v1"
    assert facts.text == json.dumps(saved, ensure_ascii=True, separators=(",", ":"))
    entries = cast(list[JsonObject], saved["fields"])
    by_path = {tuple(cast(list[str | int], entry["path"])): entry for entry in entries}
    assert len(entries) == len(by_path) == len(expected)
    assert list(by_path) == sorted(
        by_path,
        key=lambda path: json.dumps(path, ensure_ascii=True, separators=(",", ":")),
    )
    assert by_path[INFRASTRUCTURE_PATH]["reads"] == {
        "params": {"whole": [".enabled"], "presence": []}
    }
    for site in expected:
        entry = by_path[site.path]
        checked = site.checked
        assert list(entry) == ["path", "origin", "result", "rendering", "cost", "reads"]
        assert entry["origin"] == ("authored" if site.authored else "approval_default")
        assert entry["rendering"] == checked.rendering
        assert entry["result"] == (
            "string" if site.path[-1] in ("reason", "else_reason") else "bool"
        )
        cost = cast(JsonObject, entry["cost"])
        assert list(cost) == ["steps", "memory", "compile_steps"]
        assert cost == {
            "steps": checked.estimate.steps,
            "memory": checked.estimate.memory,
            "compile_steps": checked.estimate.compile_steps,
        }
        reads = cast(dict[str, JsonObject], entry["reads"])
        assert all(list(paths) == ["whole", "presence"] for paths in reads.values())
        assert reads == {
            name: {"whole": list(paths.whole), "presence": list(paths.presence)}
            for name, paths in checked.projections.items()
        }


def check_lifetime() -> None:
    owner = EvaluationThread(load_binding())
    with asyncio.Runner() as runner:
        evaluator = runner.run(open_evaluator(owner))
        try:
            cancel_delivery(runner, evaluator, prior_deliveries=0)
            cancel_delivery(runner, evaluator, prior_deliveries=FIELDS + 1)
            reused = runner.run(verify(mixed_document(), evaluator))
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())
        document = mixed_document()
        document_ref = ref(document)
        operation = verify(document, evaluator, facts_bytes=WORK)
        del document
        baseline = checked_expression_count()
        with pytest.raises(EvaluatorUnavailable) as unavailable:
            runner.run(operation)
        assert document_ref() is None
        assert checked_expression_count() == baseline
        assert pending_patterns() == 0
        assert pending_facts() == 0

    assert isinstance(reused, GraphCelChecks)
    assert reused.diagnostics.valid
    assert unavailable.value.reason is UnavailableReason.CLOSED
    assert_settled(owner)


def cancel_delivery(
    runner: asyncio.Runner, evaluator: Evaluator, *, prior_deliveries: int
) -> None:
    document = with_infrastructure(
        mixed_document(params=PARAMETER_SCHEMA), "params.enabled"
    )
    document_ref = ref(document)
    baseline = checked_expression_count()
    loop = runner.get_loop()
    owner = evaluator.owner
    task = loop.create_task(verify(document, evaluator, facts_bytes=WORK))
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
    facts_during_delivery = pending_facts()
    assert task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        loop.run_until_complete(task)

    assert delivery.outstanding == delivery.deliveries == 1
    assert document_released
    assert trees_during_delivery == (0 if prior_deliveries else FIELDS + 1)
    assert patterns_during_delivery == 1
    assert facts_during_delivery == 1
    assert cancelled.value.__context__ is None
    assert checked_expression_count() == baseline
    assert pending_patterns() == 0
    assert pending_facts() == 0
    assert_idle(owner, 1)


def check_native_refusal() -> None:
    document = mixed_document(reason=REFUSED_REASON)
    inventory = checked_graph_sites(document, DECLARATIONS, 2)
    assert isinstance(inventory, tuple)
    submitted = [site.checked.rendering for site in inventory[:REFUSED_DELIVERIES]]
    del inventory
    loaded = load_binding()
    native = RefusingCompile(loaded.native_compile)
    owner = EvaluationThread(Binding(loaded.version, loaded.panic, native))

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        evaluator = runner.run(open_evaluator(owner))
        try:
            baseline = checked_expression_count()
            refused = observe_deliveries(
                loop,
                loop.create_task(verify(document, evaluator, facts_bytes=WORK)),
                owner,
                REFUSED_DELIVERIES,
            )
            compiled = list(native.renderings)
            retained_trees = checked_expression_count() - baseline
            retained_patterns = pending_patterns()
            retained_facts = pending_facts()
            reused = observe_deliveries(
                loop,
                loop.create_task(verify(mixed_document(), evaluator)),
                owner,
                FIELDS + 1,
            )
            assert_idle(owner, 1)
        finally:
            runner.run(evaluator.close())

    assert refused == IncompleteRenderings(
        RenderingRefusal.NATIVE_REFUSED, REFUSED_PATH, Fault.VALUE_ERROR
    )
    assert REFUSAL_MARKER not in repr(refused)
    assert compiled == submitted
    assert retained_trees == retained_patterns == retained_facts == 0
    assert isinstance(reused, GraphCelChecks)
    assert reused.compiled_fields == FIELDS
    assert reused.diagnostics.valid
    assert_settled(owner)


def run(scenario: str) -> None:
    cases = {
        "mixed": check_mixed,
        "refusals": check_refusals,
        "infrastructure": check_infrastructure,
        "facts": check_facts,
        "typed-limits": check_typed_limits,
        "lifetime": check_lifetime,
        "native-refusal": check_native_refusal,
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
