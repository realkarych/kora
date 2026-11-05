import asyncio
import gc
import json
from collections.abc import Coroutine
from dataclasses import asdict
from functools import partial
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
    checked_graph_sites,
    verify_graph_cel,
)
from kora.api.graph_cel_facts import GraphCelFacts
from kora.cel_eval.binding import load_binding
from kora.cel_eval.errors import EvaluatorUnavailable, UnavailableReason
from kora.cel_eval.thread import EvaluationThread, Evaluator
from kora.db.graph_value_json import ObjectReader
from kora.ir.approval_compilation import checked_approval_sites
from kora.ir.branch_expressions import checked_branch_sites
from kora.ir.cel_inventory_admission import CheckedInventoryAdmission
from kora.ir.diagnostic_collection import DiagnosticCollection
from kora.ir.document import GraphDocument
from kora.ir.expression_fields import FieldPath
from kora.ir.json import JsonObject
from kora.ir.retry_expressions import (
    CheckedRetrySite,
    InfrastructureRetryPrerequisite,
    checked_infrastructure_retry_site,
    checked_retry_sites,
)
from kora.ir.trigger_expressions import checked_sensor_trigger_sites
from kora.ir.wait_expressions import WaitExpressionResult, checked_wait_sites
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
    *,
    rejected: bool = False,
    wait_node: dict[str, object] | None = None,
    params: JsonObject | None = None,
) -> GraphDocument:
    prefix = "(?ii)" if rejected else "^"
    escaped = r"\u0070" + PATTERN_MARKER[1:]
    predicate = f'"a".matches("{prefix}{PATTERN_MARKER}")'
    escaped_predicate = f'true || "😀".matches("{prefix}{escaped}")'
    return document_for(
        contracts=BUILTINS,
        params=params,
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


def check_refusals() -> None:
    document = mixed_document()
    single_family = branch_document(
        f'"a".matches("^{PATTERN_MARKER}")', '"reason"', '"fallback"'
    )
    invalid_wait = mixed_document(wait_node=wait("unknown", poll=True))
    missing_input = mixed_document(
        wait_node=wait("inputs.unavailable.ok", poll=True, inputs=unavailable_inputs())
    )
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
    assert isinstance(typed_refusal, WaitExpressionResult)
    assert not typed_refusal.wait_expressions_valid
    assert isinstance(unresolved, WaitExpressionResult)
    assert unresolved.context_unresolved == 1
    assert unresolved.diagnostics.valid
    assert not unresolved.wait_expressions_complete
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


def run(scenario: str) -> None:
    cases = {
        "mixed": check_mixed,
        "refusals": check_refusals,
        "infrastructure": check_infrastructure,
        "facts": check_facts,
        "typed-limits": check_typed_limits,
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
