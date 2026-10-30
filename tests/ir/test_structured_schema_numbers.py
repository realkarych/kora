import gc
import json
import math
import sys
import weakref
from collections.abc import Callable
from dataclasses import fields
from typing import cast

import pytest

from kora.ir.structured_output import (
    StructuredOutput,
    canonical_structured_output,
    structured_output_digest,
)
from kora.ir.structured_schema_evaluation_policy import PARAMETERS, EvaluationRefused
from kora.ir.structured_schema_evaluation_work import EvaluationMeter
from kora.ir.structured_schema_numbers import (
    DecimalNumber,
    decimal_number,
    numeric_compare,
    numeric_equal,
    numeric_is_integer,
    numeric_multiple_of,
)
from tests.ir.structured_output_cases import ARTIFACTS


def test_structured_schema_numbers_execute_published_scalar_vectors() -> None:
    vectors = cast(
        "dict[str, list[dict[str, object]]]",
        json.loads((ARTIFACTS / "StructuredOutput.vectors.json").read_bytes()),
    )
    cases = {
        cast("str", case["name"]): case for case in vectors["schema_arithmetic_cases"]
    }
    meter = EvaluationMeter()
    decisions: dict[str, bool] = {}

    for name in ("decimal-multiple", "wide-integer-half-multiple"):
        case = cases[name]
        schema = cast("dict[str, object]", json.loads(cast("str", case["schema_text"])))
        value: object = json.loads(cast("str", case["value_text"]))
        decisions[name] = numeric_multiple_of(value, schema["multipleOf"], meter)
    for name in ("decimal-canonical-equality", "signed-zero-equality"):
        case = cases[name]
        schema = cast("dict[str, object]", json.loads(cast("str", case["schema_text"])))
        value = json.loads(cast("str", case["value_text"]))
        decisions[name] = numeric_equal(value, schema["const"], meter)

    assert decisions == {name: cases[name]["accepted"] for name in decisions}


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        pytest.param(0.3, 0.1, True, id="decimal-multiple"),
        pytest.param(10**400, 0.5, True, id="401-digit-half-multiple"),
        pytest.param(-0.3, 0.1, True, id="negative-dividend"),
        pytest.param(0.3, 0.2, False, id="fractional-quotient"),
        pytest.param(0, 5e-324, True, id="zero-subnormal-divisor"),
        pytest.param(5e-324, 1e-323, False, id="subnormal-fraction"),
        pytest.param(1e-323, 5e-324, True, id="subnormal-double"),
        pytest.param(sys.float_info.max, 1, True, id="largest-finite-integral"),
        pytest.param((1 << 13_999) - 2, 3, True, id="14000-bit-dividend"),
    ],
)
def test_structured_schema_exact_decimal_divisibility(
    left: int | float, right: int | float, expected: bool
) -> None:
    meter = EvaluationMeter()

    result = numeric_multiple_of(left, right, meter)

    assert result is expected


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        pytest.param(1e23, 100_000_000_000_000_000_000_000, 0, id="frozen-decimal"),
        pytest.param(1e23, 99_999_999_999_999_999_999_999, 1, id="below-decimal"),
        pytest.param(1e23, 100_000_000_000_000_000_000_001, -1, id="above-decimal"),
        pytest.param(0, -0.0, 0, id="integer-negative-float-zero"),
        pytest.param(-0.0, 0.0, 0, id="signed-floating-zero"),
        pytest.param(-0.3, -0.2, -1, id="same-negative-sign"),
        pytest.param(0.3, 0.2, 1, id="same-positive-sign"),
        pytest.param(0, 5e-324, -1, id="zero-subnormal"),
        pytest.param(5e-324, 1e-323, -1, id="subnormal-order"),
        pytest.param(
            sys.float_info.max,
            17_976_931_348_623_157 * 10**292,
            0,
            id="largest-finite-decimal",
        ),
        pytest.param((1 << 14_000) - 1, (1 << 14_000) - 2, 1, id="14000-bit-order"),
        pytest.param(-(1 << 1000), -5e-324, -1, id="negative-wide-and-subnormal"),
    ],
)
def test_structured_schema_exact_decimal_ordering_and_equality(
    left: int | float, right: int | float, expected: int
) -> None:
    meter = EvaluationMeter()

    comparison = numeric_compare(left, right, meter)
    equality = numeric_equal(left, right, meter)
    reverse = numeric_compare(right, left, meter)

    assert comparison == expected
    assert equality is (expected == 0)
    assert reverse == -expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(0, True, id="integer-zero"),
        pytest.param(-0.0, True, id="negative-floating-zero"),
        pytest.param(1.0, True, id="integral-fixed-float"),
        pytest.param(-1.0, True, id="negative-integral-float"),
        pytest.param(1e23, True, id="positive-decimal-exponent"),
        pytest.param(1.5, False, id="fractional-fixed-float"),
        pytest.param(-1.5, False, id="negative-fractional-float"),
        pytest.param(5e-324, False, id="minimum-subnormal"),
        pytest.param(sys.float_info.max, True, id="maximum-finite"),
        pytest.param((1 << 14_000) - 1, True, id="maximum-integer-bits"),
    ],
)
def test_structured_schema_integer_classifier_uses_canonical_decimal(
    value: int | float, expected: bool
) -> None:
    meter = EvaluationMeter()

    result = numeric_is_integer(value, meter)

    assert result is expected


def test_structured_schema_supported_numbers_can_exhaust_alignment_work() -> None:
    meter = EvaluationMeter()

    with pytest.raises(EvaluationRefused) as rejected:
        numeric_compare(-(1 << 13_999), -5e-324, meter)

    assert rejected.value.reason == "evaluation_exhausted"
    assert meter.work <= PARAMETERS["max_evaluation_work"]
    assert meter.auxiliary_bytes <= PARAMETERS["max_auxiliary_bytes"]
    assert meter.auxiliary_entries <= PARAMETERS["max_auxiliary_entries"]


def test_structured_schema_numerical_equality_preserves_payload_identity() -> None:
    integer = StructuredOutput(1)
    floating = StructuredOutput(1.0)
    positive_zero = StructuredOutput(0.0)
    negative_zero = StructuredOutput(-0.0)
    meter = EvaluationMeter()

    same_one = numeric_equal(integer.value, floating.value, meter)
    same_zero = numeric_equal(positive_zero.value, negative_zero.value, meter)

    assert same_one
    assert same_zero
    assert canonical_structured_output(integer) == b"1"
    assert canonical_structured_output(floating) == b"1.0"
    assert canonical_structured_output(positive_zero) == b"0.0"
    assert canonical_structured_output(negative_zero) == b"-0.0"
    assert structured_output_digest(integer) != structured_output_digest(floating)
    assert structured_output_digest(positive_zero) != structured_output_digest(
        negative_zero
    )


@pytest.mark.parametrize(
    ("value", "coefficient", "exponent"),
    [
        pytest.param(14, 14, 0, id="borrowed-integer"),
        pytest.param(1.0, 10, -1, id="unstripped-trailing-zero"),
        pytest.param(-1.25, -125, -2, id="signed-fixed"),
        pytest.param(1e23, 1, 23, id="scientific"),
        pytest.param(5e-324, 5, -324, id="minimum-subnormal"),
        pytest.param(-0.0, 0, 0, id="mathematical-zero"),
    ],
)
def test_structured_schema_decimal_record_has_only_coefficient_and_exponent(
    value: int | float, coefficient: int, exponent: int
) -> None:
    meter = EvaluationMeter()

    result = decimal_number(value, meter)

    assert result.coefficient == coefficient
    assert result.exponent == exponent
    assert [field.name for field in fields(result)] == ["coefficient", "exponent"]
    assert repr(result) == "DecimalNumber()"


class AuthoredInteger(int):
    def bit_length(self) -> int:
        raise AssertionError("authored integer inspection invoked")


class AuthoredFloat(float):
    def __repr__(self) -> str:
        raise AssertionError("authored float spelling invoked")


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        AuthoredInteger(1),
        AuthoredFloat(0.3),
        math.nan,
        math.inf,
        -math.inf,
    ],
    ids=["true", "false", "integer-subclass", "float-subclass", "nan", "inf", "-inf"],
)
def test_structured_schema_numbers_refuse_non_native_or_nonfinite_scalars(
    value: object,
) -> None:
    meter = EvaluationMeter()

    with pytest.raises(EvaluationRefused) as rejected:
        decimal_number(value, meter)

    assert rejected.value.reason == "unsupported_schema"


@pytest.mark.parametrize("bits", [14_000, 14_001], ids=["inclusive", "over"])
@pytest.mark.parametrize("sign", [-1, 1], ids=["negative", "positive"])
def test_structured_schema_numbers_check_integer_bit_capacity_before_arithmetic(
    bits: int, sign: int
) -> None:
    value = sign * ((1 << bits) - 1)
    meter = EvaluationMeter()
    previous_limit = sys.get_int_max_str_digits()

    if bits > 14_000:
        with pytest.raises(EvaluationRefused) as rejected:
            decimal_number(value, meter)

        assert rejected.value.reason == "unsupported_schema"
        assert meter.auxiliary_bytes == 0
        assert sys.get_int_max_str_digits() == previous_limit
        return
    result = decimal_number(value, meter)

    assert result.coefficient is value
    assert result.exponent == 0
    assert sys.get_int_max_str_digits() == previous_limit


@pytest.mark.parametrize("divisor", [0, 0.0, -0.0, -1, -0.1])
def test_structured_schema_multiple_of_refuses_nonpositive_divisor(
    divisor: int | float,
) -> None:
    meter = EvaluationMeter()

    with pytest.raises(EvaluationRefused) as rejected:
        numeric_multiple_of(1, divisor, meter)

    assert rejected.value.reason == "unsupported_schema"
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None


def equal_one(value: object, meter: EvaluationMeter) -> bool:
    return numeric_equal(value, 1, meter)


def compare_one(value: object, meter: EvaluationMeter) -> int:
    return numeric_compare(1, value, meter)


def multiple_one(value: object, meter: EvaluationMeter) -> bool:
    return numeric_multiple_of(value, 1, meter)


@pytest.mark.parametrize(
    "operation", [numeric_is_integer, equal_one, compare_one, multiple_one]
)
def test_structured_schema_numeric_boundaries_never_invoke_authored_metaclasses(
    operation: Callable[[object, EvaluationMeter], object],
) -> None:
    calls: list[str] = []

    class AuthoredMeta(type):
        def __eq__(cls, other: object) -> bool:
            calls.append("equality")
            raise AssertionError("authored metaclass equality invoked")

    class Authored(metaclass=AuthoredMeta):
        def __repr__(self) -> str:
            calls.append("representation")
            raise AssertionError("private-numeric-canary")

    value = Authored()
    retained = weakref.ref(value)
    meter = EvaluationMeter()

    with pytest.raises(EvaluationRefused) as rejected:
        operation(value, meter)
    reason = rejected.value.reason
    safe_text = (str(rejected.value), repr(rejected.value))
    del rejected
    del value
    gc.collect()

    assert reason == "unsupported_schema"
    assert calls == []
    assert all("private-numeric-canary" not in text for text in safe_text)
    assert retained() is None


def test_structured_schema_numeric_release_after_caller_disposes_refusal() -> None:
    class Authored:
        pass

    value = Authored()
    retained = weakref.ref(value)
    meter = EvaluationMeter()

    with pytest.raises(EvaluationRefused) as rejected:
        numeric_compare(0.3, value, meter)
    del value
    gc.collect()
    held_by_caller = retained() is not None
    del rejected
    gc.collect()

    assert held_by_caller
    assert retained() is None


def test_structured_schema_numeric_records_release_after_success_and_refusal() -> None:
    gc.collect()
    original = {id(value) for value in gc.get_objects() if type(value) is DecimalNumber}
    meter = EvaluationMeter()

    same = numeric_equal(0.3, 0.3, meter)
    with pytest.raises(EvaluationRefused) as rejected:
        numeric_multiple_of(0.3, 0.0, meter)
    del rejected
    gc.collect()
    remaining = {
        id(value) for value in gc.get_objects() if type(value) is DecimalNumber
    }

    assert same
    assert remaining == original
    assert meter.auxiliary_entries > 0
    assert meter.auxiliary_bytes > 0
    assert meter.work < PARAMETERS["max_evaluation_work"]
