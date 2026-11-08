import math
from dataclasses import replace
from traceback import format_exception, walk_tb
from typing import Literal, cast

import pytest

from kora.db.graph_value_json import ObjectReader
from kora.engine.graph_parameters import (
    build_graph_parameter_binding,
    read_graph_parameter_binding,
)
from kora.engine.parameter_numeric_predicates import (
    ParameterNumericFailure,
    ParameterNumericPredicate,
    ParameterNumericRejected,
    check_parameter_numeric_predicate,
)
from kora.engine.start_records import prepare_manual_start_request
from kora.ir.json import JsonObject
from kora.ir.structured_schema_evaluation_work import (
    BoundedEvaluationMeter,
    EvaluationLimits,
    EvaluationMeter,
)
from tests.engine.test_graph_parameters import parameter_document
from tests.engine.test_start_records import GRAPH, NAMESPACE

LIMITS = EvaluationLimits(40_000, 4_096, 128, 14_001, 16_384)
PRIVATE = "private-parameter-numeric-value"
METERED_PAIR_LIMITS = EvaluationLimits(82, 4, 4, 64, 64)


def checked_operands(
    value: int | float, operand: int | float, predicate: ParameterNumericPredicate
) -> tuple[object, object]:
    request = prepare_manual_start_request(NAMESPACE, GRAPH, {"value": value})
    schema = parameter_document({"properties": {"value": {predicate.value: operand}}})
    text = build_graph_parameter_binding(schema, max_encoded_bytes=10_000)
    saved = ObjectReader(text).object()
    read_graph_parameter_binding(schema, saved, max_encoded_bytes=10_000)
    properties = cast(JsonObject, cast(JsonObject, saved["schema"])["properties"])
    return (
        request.parameters()["value"],
        cast(JsonObject, properties["value"])[predicate.value],
    )


@pytest.mark.parametrize(
    ("value", "divisor", "expected"),
    [
        (0.3, 0.1, True),
        (0.30000000000000004, 0.1, False),
        (2**63 - 1, 2.0, False),
        (-0.0, 0.1, True),
        (-(2**63), 2.0, True),
    ],
)
def test_parameter_multiple_of_uses_exact_canonical_decimals(
    value: int | float, divisor: float, expected: bool
) -> None:
    predicate = ParameterNumericPredicate.MULTIPLE_OF
    actual, saved_divisor = checked_operands(value, divisor, predicate)
    meter = BoundedEvaluationMeter(limits=LIMITS)

    matches = check_parameter_numeric_predicate(
        predicate, actual, saved_divisor, meter=meter
    )

    assert matches is expected
    assert type(actual) is type(value)
    assert math.copysign(1, actual) == math.copysign(1, value)


@pytest.mark.parametrize(
    ("predicate", "value", "operand", "expected"),
    [
        (ParameterNumericPredicate.INTEGER, 1.0, None, True),
        (ParameterNumericPredicate.INTEGER, 1.1, None, False),
        (ParameterNumericPredicate.INTEGER, True, None, False),
        (ParameterNumericPredicate.EQUAL, 0, -0.0, True),
        (ParameterNumericPredicate.EQUAL, True, 1, False),
        (ParameterNumericPredicate.EQUAL, 1, True, False),
        (ParameterNumericPredicate.EQUAL, 2**63 - 1, float(2**63), False),
        (ParameterNumericPredicate.MINIMUM, 1, 1.0, True),
        (ParameterNumericPredicate.EXCLUSIVE_MINIMUM, 1.0, 1, False),
        (ParameterNumericPredicate.EXCLUSIVE_MINIMUM, 1, -1, True),
        (ParameterNumericPredicate.MAXIMUM, 0.3, 0.3, True),
        (ParameterNumericPredicate.EXCLUSIVE_MAXIMUM, 0.3, 0.3, False),
        (ParameterNumericPredicate.EXCLUSIVE_MAXIMUM, 0.3, 0.30000000000000004, True),
        (ParameterNumericPredicate.MAXIMUM, 1, -1, False),
        (ParameterNumericPredicate.MINIMUM, False, None, True),
        (ParameterNumericPredicate.MULTIPLE_OF, "1", 0, True),
    ],
)
def test_parameter_numeric_predicates_follow_json_kinds_and_bounds(
    predicate: ParameterNumericPredicate,
    value: object,
    operand: object,
    expected: bool,
) -> None:
    meter = BoundedEvaluationMeter(limits=LIMITS)

    matches = check_parameter_numeric_predicate(predicate, value, operand, meter=meter)

    assert matches is expected


def test_parameter_numeric_predicate_accepts_pinned_integer_above_artifact_cap() -> (
    None
):
    predicate = ParameterNumericPredicate.MAXIMUM
    value, pinned = checked_operands(2**63 - 1, 1 << 14_000, predicate)
    exact = BoundedEvaluationMeter(limits=LIMITS)
    short = BoundedEvaluationMeter(limits=replace(LIMITS, max_integer_bits=14_000))

    matches = check_parameter_numeric_predicate(predicate, value, pinned, meter=exact)
    with pytest.raises(ParameterNumericRejected) as refusal:
        check_parameter_numeric_predicate(predicate, value, pinned, meter=short)

    assert matches is True
    assert type(pinned) is int
    assert pinned.bit_length() == 14_001
    assert refusal.value.reason is ParameterNumericFailure.CAPACITY


def test_parameter_numeric_predicate_bounds_intermediate_scaling_separately() -> None:
    predicate = ParameterNumericPredicate.EXCLUSIVE_MAXIMUM
    exact = BoundedEvaluationMeter(limits=replace(LIMITS, max_intermediate_bits=15))
    short = BoundedEvaluationMeter(limits=replace(LIMITS, max_intermediate_bits=14))

    matches = check_parameter_numeric_predicate(predicate, 0.3, 1000, meter=exact)
    with pytest.raises(ParameterNumericRejected) as refusal:
        check_parameter_numeric_predicate(predicate, 0.3, 1000, meter=short)

    assert matches is True
    assert refusal.value.reason is ParameterNumericFailure.CAPACITY


def test_parameter_numeric_predicates_share_exact_cumulative_allowance() -> None:
    predicate = ParameterNumericPredicate.MULTIPLE_OF
    meter = BoundedEvaluationMeter(limits=METERED_PAIR_LIMITS)

    first = check_parameter_numeric_predicate(predicate, 6, 3, meter=meter)
    second = check_parameter_numeric_predicate(predicate, 5, 3, meter=meter)

    assert (first, second) == (True, False)
    assert (meter.work, meter.auxiliary_bytes, meter.auxiliary_entries) == (82, 4, 4)


@pytest.mark.parametrize(
    ("dimension", "allowance"),
    [("max_work", 81), ("max_auxiliary_bytes", 3), ("max_auxiliary_entries", 3)],
)
def test_parameter_numeric_predicate_refuses_each_short_cumulative_allowance(
    dimension: Literal["max_work", "max_auxiliary_bytes", "max_auxiliary_entries"],
    allowance: int,
) -> None:
    predicate = ParameterNumericPredicate.MULTIPLE_OF
    meter = BoundedEvaluationMeter(
        limits=replace(METERED_PAIR_LIMITS, **{dimension: allowance})
    )

    first = check_parameter_numeric_predicate(predicate, 6, 3, meter=meter)
    with pytest.raises(ParameterNumericRejected) as refusal:
        check_parameter_numeric_predicate(predicate, 5, 3, meter=meter)

    assert first is True
    assert refusal.value.reason is ParameterNumericFailure.CAPACITY


def test_parameter_numeric_predicate_requires_explicit_nonartifact_meter() -> None:
    meter = EvaluationMeter()

    with pytest.raises(ParameterNumericRejected) as refusal:
        check_parameter_numeric_predicate(
            ParameterNumericPredicate.INTEGER, 1, None, meter=meter
        )

    assert refusal.value.reason is ParameterNumericFailure.INVALID
    assert meter.work == 0


@pytest.mark.parametrize("divisor", [False, 0, -1, -0.0, math.inf, math.nan, PRIVATE])
def test_parameter_numeric_predicate_refuses_invalid_entered_divisor(
    divisor: object,
) -> None:
    predicate = ParameterNumericPredicate.MULTIPLE_OF
    meter = BoundedEvaluationMeter(limits=LIMITS)

    with pytest.raises(ParameterNumericRejected) as refusal:
        check_parameter_numeric_predicate(predicate, 6, divisor, meter=meter)

    assert refusal.value.reason is ParameterNumericFailure.INVALID


class _ProtectedKind(type):
    def __eq__(self, other: object) -> bool:
        raise RuntimeError(PRIVATE)


class _ProtectedOperand(metaclass=_ProtectedKind):
    pass


@pytest.mark.parametrize(
    ("left", "right"),
    [(math.nan, True), (True, math.inf), (None, _ProtectedOperand())],
)
def test_parameter_numeric_equality_refuses_malformed_mixed_kind_operands(
    left: object, right: object
) -> None:
    meter = BoundedEvaluationMeter(limits=LIMITS)

    with pytest.raises(ParameterNumericRejected) as refusal:
        check_parameter_numeric_predicate(
            ParameterNumericPredicate.EQUAL, left, right, meter=meter
        )

    assert refusal.value.reason is ParameterNumericFailure.INVALID


@pytest.mark.parametrize(
    ("work", "expected"),
    [(40_000, ParameterNumericFailure.INVALID), (0, ParameterNumericFailure.CAPACITY)],
)
def test_parameter_numeric_refusal_clears_operands_and_active_exception(
    work: int, expected: ParameterNumericFailure
) -> None:
    meter = BoundedEvaluationMeter(limits=replace(LIMITS, max_work=work))
    operand = _ProtectedOperand()

    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        with pytest.raises(ParameterNumericRejected) as refusal:
            check_parameter_numeric_predicate(
                ParameterNumericPredicate.MINIMUM, 1, operand, meter=meter
            )

    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }
    assert refusal.value.reason is expected
    assert refusal.value.__cause__ is refusal.value.__context__ is None
    assert frames["check_parameter_numeric_predicate"] == {"reason": expected}
    assert {"_check", "_is_number"}.isdisjoint(frames)
    assert PRIVATE not in "".join(format_exception(refusal.value))
