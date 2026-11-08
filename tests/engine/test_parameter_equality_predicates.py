from dataclasses import replace
from traceback import format_exception, walk_tb
from typing import Literal, cast

import pytest

from kora.db.graph_value_json import ObjectReader
from kora.engine.graph_parameters import (
    build_graph_parameter_binding,
    read_graph_parameter_binding,
)
from kora.engine.parameter_arena_predicates import (
    ParameterArenaFailure,
    ParameterArenaPredicate,
    ParameterArenaRejected,
    check_parameter_arena_predicate,
)
from kora.engine.start_records import prepare_manual_start_request
from kora.ir.json import JsonObject
from kora.ir.structured_schema_cursors import ArenaCursor
from kora.ir.structured_schema_evaluation_work import (
    BoundedEvaluationMeter,
    EvaluationLimits,
)
from tests.engine.test_graph_parameters import parameter_document
from tests.engine.test_start_records import GRAPH, NAMESPACE
from tests.ir.structured_output_cases import nested
from tests.ir.test_structured_schema_equality import frozen_literal

LIMITS = EvaluationLimits(40_000, 4_096, 1_024, 14_000, 16_384)
EXACT_FLOAT_PAIR = EvaluationLimits(8_390, 100, 9, 64, 64)
PRIVATE = "private-manual-equality-operand"


def literal_cursor(value: object) -> ArenaCursor:
    literal = frozen_literal(value)
    return ArenaCursor(literal.nodes, literal.root)


def checked_pair(
    predicate: ParameterArenaPredicate, value: object, operand: object
) -> tuple[ArenaCursor, ArenaCursor]:
    request = prepare_manual_start_request(NAMESPACE, GRAPH, {"value": value})
    document = parameter_document({"properties": {"value": {predicate.value: operand}}})
    text = build_graph_parameter_binding(document, max_encoded_bytes=10_000)
    saved = ObjectReader(text).object()
    read_graph_parameter_binding(document, saved, max_encoded_bytes=10_000)
    properties = cast(JsonObject, cast(JsonObject, saved["schema"])["properties"])
    return (
        literal_cursor(request.parameters()["value"]),
        literal_cursor(cast(JsonObject, properties["value"])[predicate.value]),
    )


@pytest.mark.parametrize(
    ("value", "operand", "expected"),
    [
        pytest.param(
            {"z": [None], "a": {"\x00": 1.0}},
            {"a": {"\x00": 1}, "z": [None]},
            True,
            id="canonical-object-keys",
        ),
        pytest.param([False, 1], [1, False], False, id="array-order"),
        pytest.param([True], [1], False, id="boolean-and-number"),
        pytest.param(1e23, 10**23, True, id="exact-canonical-decimal"),
        pytest.param(1e23, int(1e23), False, id="binary-rounding"),
        pytest.param(["é"], ["e\u0301"], False, id="no-normalization"),
    ],
)
def test_parameter_const_checks_ordered_structural_equality(
    value: object, operand: object, expected: bool
) -> None:
    predicate = ParameterArenaPredicate.CONST
    left, right = checked_pair(predicate, value, operand)
    meter = BoundedEvaluationMeter(limits=LIMITS)

    matches = check_parameter_arena_predicate(
        predicate, left, right, meter=meter, max_pair_frames=8
    )

    assert matches is expected


def test_parameter_const_compares_all_object_keys_before_values() -> None:
    predicate = ParameterArenaPredicate.CONST
    left, right = checked_pair(predicate, {"a": 0.3, "z": True}, {"a": 0.3, "y": True})
    meter = BoundedEvaluationMeter(limits=replace(LIMITS, max_work=400))

    matches = check_parameter_arena_predicate(
        predicate, left, right, meter=meter, max_pair_frames=8
    )

    assert matches is False


@pytest.mark.parametrize(
    ("predicate", "value", "operand", "expected"),
    [
        pytest.param(ParameterArenaPredicate.ENUM, 1, [1, 1.0], True, id="enum"),
        pytest.param(
            ParameterArenaPredicate.UNIQUE_ITEMS,
            [1, 1, 1.0],
            True,
            False,
            id="unique",
        ),
    ],
)
def test_parameter_equality_stops_after_first_decisive_pair(
    predicate: ParameterArenaPredicate,
    value: object,
    operand: object,
    expected: bool,
) -> None:
    left, right = checked_pair(predicate, value, operand)
    meter = BoundedEvaluationMeter(limits=replace(LIMITS, max_work=400))

    matches = check_parameter_arena_predicate(
        predicate, left, right, meter=meter, max_pair_frames=8
    )

    assert matches is expected


@pytest.mark.parametrize(
    ("predicate", "value", "operand"),
    [
        pytest.param(ParameterArenaPredicate.ENUM, 1, [1.0, 1], id="enum"),
        pytest.param(
            ParameterArenaPredicate.UNIQUE_ITEMS,
            [1, 1.0, 1],
            True,
            id="unique",
        ),
    ],
)
def test_parameter_equality_cannot_skip_an_incomplete_entered_pair(
    predicate: ParameterArenaPredicate, value: object, operand: object
) -> None:
    left, right = checked_pair(predicate, value, operand)
    meter = BoundedEvaluationMeter(limits=replace(LIMITS, max_work=400))

    with pytest.raises(ParameterArenaRejected) as refusal:
        check_parameter_arena_predicate(
            predicate, left, right, meter=meter, max_pair_frames=8
        )

    assert refusal.value.reason is ParameterArenaFailure.CAPACITY


@pytest.mark.parametrize(
    ("predicate", "value", "operand", "expected"),
    [
        (ParameterArenaPredicate.ENUM, {"x": [1]}, [{"x": [2]}, {"x": [1.0]}], True),
        (ParameterArenaPredicate.ENUM, 1, [True, 2], False),
        (ParameterArenaPredicate.UNIQUE_ITEMS, [True, 1], True, True),
        (ParameterArenaPredicate.UNIQUE_ITEMS, [{"a": -0.0}, {"a": 0}], True, False),
        (ParameterArenaPredicate.UNIQUE_ITEMS, [1, 1.0], False, True),
        (ParameterArenaPredicate.UNIQUE_ITEMS, {"a": 1}, "unentered", True),
    ],
)
def test_parameter_collection_equality_respects_predicate_applicability(
    predicate: ParameterArenaPredicate,
    value: object,
    operand: object,
    expected: bool,
) -> None:
    left, right = checked_pair(predicate, value, operand)
    meter = BoundedEvaluationMeter(limits=LIMITS)

    matches = check_parameter_arena_predicate(
        predicate, left, right, meter=meter, max_pair_frames=8
    )

    assert matches is expected


def test_parameter_equality_uses_caller_pair_depth_above_artifact_default() -> None:
    predicate = ParameterArenaPredicate.CONST
    value = nested(18)
    left, right = checked_pair(predicate, value, value)
    exact = BoundedEvaluationMeter(limits=LIMITS)
    short = BoundedEvaluationMeter(limits=LIMITS)

    matches = check_parameter_arena_predicate(
        predicate, left, right, meter=exact, max_pair_frames=19
    )
    with pytest.raises(ParameterArenaRejected) as refusal:
        check_parameter_arena_predicate(
            predicate, left, right, meter=short, max_pair_frames=18
        )

    assert matches is True
    assert refusal.value.reason is ParameterArenaFailure.CAPACITY


def test_parameter_equality_admits_exact_float_pair_work_bytes_and_entries() -> None:
    predicate = ParameterArenaPredicate.CONST
    left, right = checked_pair(predicate, 0.3, 0.3)
    meter = BoundedEvaluationMeter(limits=EXACT_FLOAT_PAIR)

    matches = check_parameter_arena_predicate(
        predicate, left, right, meter=meter, max_pair_frames=1
    )

    assert matches is True
    assert (meter.work, meter.auxiliary_bytes, meter.auxiliary_entries) == (
        8_390,
        100,
        9,
    )


def test_parameter_equality_shares_allowance_across_completed_and_refused_calls() -> (
    None
):
    predicate = ParameterArenaPredicate.CONST
    left, right = checked_pair(predicate, None, None)
    meter = BoundedEvaluationMeter(limits=replace(LIMITS, max_work=103))

    first = check_parameter_arena_predicate(
        predicate, left, right, meter=meter, max_pair_frames=1
    )
    paid = meter.work
    with pytest.raises(ParameterArenaRejected) as refusal:
        check_parameter_arena_predicate(
            predicate, left, right, meter=meter, max_pair_frames=1
        )

    assert first is True
    assert paid == 52
    assert meter.work == 103
    assert refusal.value.reason is ParameterArenaFailure.CAPACITY


@pytest.mark.parametrize(
    ("dimension", "allowance"),
    [("max_work", 8_389), ("max_auxiliary_bytes", 99), ("max_auxiliary_entries", 8)],
)
def test_parameter_equality_refuses_independently_short_float_pair_allowance(
    dimension: Literal["max_work", "max_auxiliary_bytes", "max_auxiliary_entries"],
    allowance: int,
) -> None:
    predicate = ParameterArenaPredicate.CONST
    left, right = checked_pair(predicate, 0.3, 0.3)
    meter = BoundedEvaluationMeter(
        limits=replace(EXACT_FLOAT_PAIR, **{dimension: allowance})
    )

    with pytest.raises(ParameterArenaRejected) as refusal:
        check_parameter_arena_predicate(
            predicate, left, right, meter=meter, max_pair_frames=1
        )

    assert refusal.value.reason is ParameterArenaFailure.CAPACITY


def test_parameter_equality_refuses_negative_cursor_index() -> None:
    predicate = ParameterArenaPredicate.CONST
    left, right = checked_pair(predicate, PRIVATE, PRIVATE)
    invalid = replace(right, node=-1)
    meter = BoundedEvaluationMeter(limits=LIMITS)

    with pytest.raises(ParameterArenaRejected) as refusal:
        check_parameter_arena_predicate(
            predicate, left, invalid, meter=meter, max_pair_frames=1
        )

    assert refusal.value.reason is ParameterArenaFailure.INVALID


def test_parameter_equality_clears_active_pair_and_private_exception_context() -> None:
    predicate = ParameterArenaPredicate.CONST
    left, right = checked_pair(predicate, [PRIVATE], [PRIVATE])
    meter = BoundedEvaluationMeter(limits=LIMITS)

    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        with pytest.raises(ParameterArenaRejected) as refusal:
            check_parameter_arena_predicate(
                predicate, left, right, meter=meter, max_pair_frames=1
            )

    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }
    assert refusal.value.reason is ParameterArenaFailure.CAPACITY
    assert refusal.value.__cause__ is refusal.value.__context__ is None
    assert frames["check_parameter_arena_predicate"] == {
        "reason": ParameterArenaFailure.CAPACITY
    }
    assert {"_check", "_push", "compare_json_cursors"}.isdisjoint(frames)
    assert PRIVATE not in "".join(format_exception(refusal.value))
