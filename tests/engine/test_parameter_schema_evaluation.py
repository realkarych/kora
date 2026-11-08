import gc
from dataclasses import replace
from traceback import format_exception, walk_tb
from types import GeneratorType
from typing import cast

import pytest

from kora.db.graph_value_json import ObjectReader
from kora.engine.graph_parameters import (
    build_graph_parameter_binding,
    read_graph_parameter_binding,
)
from kora.engine.parameter_schema_evaluation import (
    ParameterSchemaEvaluationFailure,
    ParameterSchemaEvaluationRejected,
    evaluate_parameter_schema,
)
from kora.engine.start_records import prepare_manual_start_request
from kora.ir.json import JsonObject
from kora.ir.structured_schema_cursors import ArenaCursor
from kora.ir.structured_schema_evaluation_work import (
    BoundedEvaluationMeter,
    EvaluationLimits,
)
from tests.engine.test_graph_parameters import parameter_document
from tests.engine.test_parameter_equality_predicates import LIMITS, literal_cursor
from tests.engine.test_start_records import GRAPH, NAMESPACE

PRIVATE = "private-parameter-schema-evaluation"
WIDE_LIMITS = replace(LIMITS, max_work=300_000, max_auxiliary_entries=10_000)


def checked_schema(value: object, schema: object) -> tuple[ArenaCursor, ArenaCursor]:
    request = prepare_manual_start_request(NAMESPACE, GRAPH, {"value": value})
    document = parameter_document({"properties": {"value": schema}})
    text = build_graph_parameter_binding(document, max_encoded_bytes=10_000)
    saved = ObjectReader(text).object()
    read_graph_parameter_binding(document, saved, max_encoded_bytes=10_000)
    properties = cast(JsonObject, cast(JsonObject, saved["schema"])["properties"])
    return literal_cursor(properties["value"]), literal_cursor(
        request.parameters()["value"]
    )


@pytest.mark.parametrize(
    ("value", "schema", "expected"),
    [
        (1, True, True),
        (1, False, False),
        (1, {}, True),
        (1, {"type": "integer", "minimum": 0, "const": 1.0}, True),
        (1, {"type": "integer", "exclusiveMinimum": 1, "const": 1}, False),
        (0.3, {"type": "number", "multipleOf": 0.1}, True),
        (2**63 - 1, {"multipleOf": 2.0}, False),
        ({"a": None}, {"required": ["a"], "maxProperties": 1}, True),
        ({"a": False}, {"dependentRequired": {"a": ["b"]}}, False),
        (1, {"allOf": [True, {"type": "integer"}]}, True),
        (1, {"allOf": [True, False]}, False),
        (1, {"anyOf": [False, {"minimum": 1}]}, True),
        (1, {"anyOf": [False, {"exclusiveMaximum": 1}]}, False),
        (1, {"oneOf": [False, True, False]}, True),
        (1, {"oneOf": [False, False]}, False),
        (1, {"oneOf": [True, True]}, False),
        (1, {"not": {"type": "string"}}, True),
        (1, {"not": {"type": "number"}}, False),
        (1, {"if": True, "then": False, "else": True}, False),
        (1, {"if": False, "then": True, "else": False}, False),
        (1, {"if": False, "then": False}, True),
        (1, {"if": True, "else": False}, True),
        (1, {"if": True, "then": True, "else": {"$ref": "#"}}, True),
        (1, {"if": False, "then": {"$ref": "#"}, "else": True}, True),
        ({"a": 1}, {"properties": {"a": False}, "allOf": [False]}, False),
        (
            {"a": 1},
            {
                "$recursiveRef": "#",
                "$vocabulary": {"urn:kora:example": True},
                "dependencies": {"a": False},
                "else": False,
                "minContains": 1,
                "then": False,
                "x-extension": {"$ref": "#"},
            },
            True,
        ),
    ],
)
def test_parameter_schema_evaluates_direct_and_logical_assertions(
    value: object, schema: object, expected: bool
) -> None:
    rule, instance = checked_schema(value, schema)
    meter = BoundedEvaluationMeter(limits=WIDE_LIMITS)

    matches = evaluate_parameter_schema(
        rule, instance, meter=meter, max_schema_frames=8, max_pair_frames=8
    )

    assert matches is expected


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"allOf": [False, {"not": False}]}, False),
        ({"anyOf": [True, {"not": False}]}, True),
        ({"oneOf": [True, True, {"not": False}]}, False),
        ({"if": {"not": False}}, True),
    ],
)
def test_parameter_schema_skips_decided_and_orphan_children(
    schema: object, expected: bool
) -> None:
    rule, instance = checked_schema(1, schema)
    meter = BoundedEvaluationMeter(limits=WIDE_LIMITS)

    matches = evaluate_parameter_schema(
        rule, instance, meter=meter, max_schema_frames=2, max_pair_frames=0
    )

    assert matches is expected


@pytest.mark.parametrize(
    "schema",
    [
        {"allOf": [{"not": False}, False]},
        {"anyOf": [{"not": False}, True]},
        {"oneOf": [True, {"not": False}, True]},
        {"if": {"not": False}, "then": True, "else": True},
        {"not": {"not": True}},
    ],
)
def test_parameter_schema_does_not_use_later_results_after_entered_refusal(
    schema: object,
) -> None:
    rule, instance = checked_schema(1, schema)
    meter = BoundedEvaluationMeter(limits=WIDE_LIMITS)

    with pytest.raises(ParameterSchemaEvaluationRejected) as refusal:
        evaluate_parameter_schema(
            rule, instance, meter=meter, max_schema_frames=2, max_pair_frames=0
        )

    assert refusal.value.reason is ParameterSchemaEvaluationFailure.CAPACITY


def test_parameter_schema_checks_type_before_unavailable_or_costly_heads() -> None:
    unavailable, instance = checked_schema(1, {"$ref": "#", "type": "string"})
    costly, _ = checked_schema(1, {"const": 1.0, "type": "string"})
    meter = BoundedEvaluationMeter(limits=replace(LIMITS, max_work=600))

    ref_matches = evaluate_parameter_schema(
        unavailable, instance, meter=meter, max_schema_frames=1, max_pair_frames=0
    )
    const_matches = evaluate_parameter_schema(
        costly, instance, meter=meter, max_schema_frames=1, max_pair_frames=0
    )

    assert ref_matches is False
    assert const_matches is False


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#"},
        {"$dynamicRef": "#"},
        {"additionalProperties": False},
        {"dependentSchemas": {"x": False}},
        {"pattern": PRIVATE},
        {"patternProperties": {PRIVATE: False}},
        {"properties": {"a": False}},
        {"propertyNames": False},
        {"unevaluatedItems": False},
        {"unevaluatedProperties": False},
        {"if": True, "then": {"$ref": "#"}},
    ],
)
def test_parameter_schema_refuses_entered_unavailable_heads(schema: object) -> None:
    rule, instance = checked_schema([], schema)
    meter = BoundedEvaluationMeter(limits=WIDE_LIMITS)

    with pytest.raises(ParameterSchemaEvaluationRejected) as refusal:
        evaluate_parameter_schema(
            rule, instance, meter=meter, max_schema_frames=4, max_pair_frames=0
        )

    assert refusal.value.reason is ParameterSchemaEvaluationFailure.UNAVAILABLE


@pytest.mark.parametrize(
    ("schema_frames", "pair_frames", "admitted"),
    [(2, 2, True), (1, 2, False), (2, 1, False)],
)
def test_parameter_schema_and_equality_have_independent_root_inclusive_frames(
    schema_frames: int, pair_frames: int, admitted: bool
) -> None:
    rule, instance = checked_schema([1], {"allOf": [{"const": [1]}]})
    meter = BoundedEvaluationMeter(limits=WIDE_LIMITS)

    if admitted:
        matches = evaluate_parameter_schema(
            rule,
            instance,
            meter=meter,
            max_schema_frames=schema_frames,
            max_pair_frames=pair_frames,
        )

        assert matches is True
        return
    with pytest.raises(ParameterSchemaEvaluationRejected) as refusal:
        evaluate_parameter_schema(
            rule,
            instance,
            meter=meter,
            max_schema_frames=schema_frames,
            max_pair_frames=pair_frames,
        )

    assert refusal.value.reason is ParameterSchemaEvaluationFailure.CAPACITY


def test_parameter_boolean_schema_has_exact_work_and_entry_admission() -> None:
    rule, instance = checked_schema(None, True)
    limits = EvaluationLimits(34, 0, 5, 0, 0)
    exact = BoundedEvaluationMeter(limits=limits)
    short_work = BoundedEvaluationMeter(limits=replace(limits, max_work=33))
    short_entries = BoundedEvaluationMeter(
        limits=replace(limits, max_auxiliary_entries=4)
    )
    no_frames = BoundedEvaluationMeter(limits=limits)

    matches = evaluate_parameter_schema(
        rule, instance, meter=exact, max_schema_frames=1, max_pair_frames=0
    )
    with pytest.raises(ParameterSchemaEvaluationRejected) as work_refusal:
        evaluate_parameter_schema(
            rule, instance, meter=short_work, max_schema_frames=1, max_pair_frames=0
        )
    with pytest.raises(ParameterSchemaEvaluationRejected) as entry_refusal:
        evaluate_parameter_schema(
            rule, instance, meter=short_entries, max_schema_frames=1, max_pair_frames=0
        )
    with pytest.raises(ParameterSchemaEvaluationRejected) as frame_refusal:
        evaluate_parameter_schema(
            rule, instance, meter=no_frames, max_schema_frames=0, max_pair_frames=0
        )

    assert matches is True
    assert (exact.work, exact.auxiliary_bytes, exact.auxiliary_entries) == (34, 0, 5)
    assert work_refusal.value.reason is ParameterSchemaEvaluationFailure.CAPACITY
    assert entry_refusal.value.reason is ParameterSchemaEvaluationFailure.CAPACITY
    assert frame_refusal.value.reason is ParameterSchemaEvaluationFailure.CAPACITY


def test_parameter_schema_shares_exact_work_across_entered_direct_branches() -> None:
    rule, instance = checked_schema(1, {"allOf": [{"maximum": 1}, {"const": 1.0}]})
    limits = EvaluationLimits(9_203, 51, 31, 64, 64)
    exact = BoundedEvaluationMeter(limits=limits)
    short = BoundedEvaluationMeter(limits=replace(limits, max_work=9_202))

    matches = evaluate_parameter_schema(
        rule, instance, meter=exact, max_schema_frames=2, max_pair_frames=1
    )
    with pytest.raises(ParameterSchemaEvaluationRejected) as refusal:
        evaluate_parameter_schema(
            rule, instance, meter=short, max_schema_frames=2, max_pair_frames=1
        )

    assert matches is True
    assert (exact.work, exact.auxiliary_bytes, exact.auxiliary_entries) == (
        9_203,
        51,
        31,
    )
    assert refusal.value.reason is ParameterSchemaEvaluationFailure.CAPACITY


def owned_continuations() -> set[int]:
    owned: set[int] = set()
    for value in gc.get_objects():
        identity = id(value)
        if isinstance(value, GeneratorType) and value.gi_code.co_filename.endswith(
            "/parameter_schema_evaluation.py"
        ):
            owned.add(identity)
    return owned


@pytest.mark.parametrize("value", [True, False])
def test_parameter_schema_releases_continuations_after_nested_result(
    value: bool,
) -> None:
    rule, instance = checked_schema(1, {"if": True, "then": {"allOf": [value]}})
    meter = BoundedEvaluationMeter(limits=WIDE_LIMITS)
    before = owned_continuations()

    matches = evaluate_parameter_schema(
        rule, instance, meter=meter, max_schema_frames=4, max_pair_frames=0
    )

    assert matches is value
    assert owned_continuations() == before


@pytest.mark.parametrize(
    ("schema_frames", "work"),
    [(2, WIDE_LIMITS.max_work), (4, 353)],
    ids=["depth-refusal", "work-exhausted"],
)
def test_parameter_schema_refusal_releases_pending_frames_and_private_context(
    schema_frames: int, work: int
) -> None:
    rule, instance = checked_schema(PRIVATE, {"allOf": [{"not": {"const": PRIVATE}}]})
    meter = BoundedEvaluationMeter(limits=replace(WIDE_LIMITS, max_work=work))
    before = owned_continuations()

    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        with pytest.raises(ParameterSchemaEvaluationRejected) as refusal:
            evaluate_parameter_schema(
                rule,
                instance,
                meter=meter,
                max_schema_frames=schema_frames,
                max_pair_frames=0,
            )

    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }
    assert refusal.value.reason is ParameterSchemaEvaluationFailure.CAPACITY
    assert refusal.value.__cause__ is refusal.value.__context__ is None
    assert frames["evaluate_parameter_schema"] == {
        "reason": ParameterSchemaEvaluationFailure.CAPACITY
    }
    assert {"_evaluate", "run", "_push", "_visit", "_sequence"}.isdisjoint(frames)
    assert PRIVATE not in "".join(format_exception(refusal.value))
    assert owned_continuations() == before
    if schema_frames == 4:
        assert meter.work == work
        assert meter.auxiliary_entries == 9
