from typing import Literal

import pytest

from kora.ir.structured_schema_evaluation_policy import PARAMETERS, EvaluationRefused
from kora.ir.structured_schema_evaluation_work import EvaluationMeter
from kora.ir.structured_schema_numbers import decimal_number, numeric_compare

Dimension = Literal["work", "bytes", "entries"]
NATIVE_INTEGER_CONVERSION_WORK: int = 2 + 2 + 1
DECIMAL_TENTHS_CONVERSION_WORK: int = (
    4 + (2 + 4096) + (1 + 3) + (1 + 24) + 1 + (1 + 2 * 2) + 9
)
MINIMUM_SUBNORMAL_CONVERSION_WORK: int = (
    4 + (2 + 4096) + (1 + 6) + (1 + 24) + 1 + (1 + 1) + (1 + 3 * 3) + 12
)


def counters(meter: EvaluationMeter) -> tuple[int, int, int]:
    return meter.work, meter.auxiliary_bytes, meter.auxiliary_entries


def remaining_meter(dimension: Dimension, remaining: int) -> EvaluationMeter:
    return EvaluationMeter(
        work=PARAMETERS["max_evaluation_work"] - remaining
        if dimension == "work"
        else 0,
        auxiliary_bytes=PARAMETERS["max_auxiliary_bytes"] - remaining
        if dimension == "bytes"
        else 0,
        auxiliary_entries=PARAMETERS["max_auxiliary_entries"] - remaining
        if dimension == "entries"
        else 0,
    )


def primitive(name: str, meter: EvaluationMeter) -> object:
    if name == "compare":
        return meter.integer_compare(1 << 30, 1)
    if name == "zero":
        return meter.integer_zero(0)
    if name == "negative":
        return meter.integer_negative(-1)
    if name == "negate":
        return meter.integer_negate(-(1 << 30))
    if name == "add":
        return meter.integer_add(1 << 30, 1)
    if name == "subtract":
        return meter.integer_subtract(1 << 30, 1)
    if name == "multiply":
        return meter.integer_multiply(1 << 30, 10)
    if name == "divmod":
        return meter.integer_divmod(1000, 7)
    if name == "remainder":
        return meter.integer_remainder(1000, 7)
    if name == "scale":
        return meter.integer_scale(3, 2)
    if name == "digits":
        return meter.parse_digits("123")
    if name == "result":
        return meter.result(3)
    raise ValueError("unknown arithmetic primitive")


def intermediate(
    operation: str, left: int, right: int, meter: EvaluationMeter
) -> object:
    if operation == "multiply":
        return meter.integer_multiply(left, right)
    if operation == "add":
        return meter.integer_add(left, right)
    if operation == "scale":
        return meter.integer_scale(left, 0)
    if operation == "divmod":
        return meter.integer_divmod(left, right)
    raise ValueError("unknown intermediate operation")


@pytest.mark.parametrize("dimension", ["work", "bytes", "entries"])
def test_structured_schema_evaluation_reservations_are_inclusive_and_atomic(
    dimension: Dimension,
) -> None:
    meter = remaining_meter(dimension, 3)
    before = counters(meter)

    meter.reserve(work=3, byte_count=3, entries=3)
    exact = counters(meter)
    with pytest.raises(EvaluationRefused) as rejected:
        meter.reserve(work=1, byte_count=1, entries=1)

    assert exact == tuple(value + 3 for value in before)
    assert counters(meter) == exact
    assert rejected.value.reason == "evaluation_exhausted"


@pytest.mark.parametrize(
    ("name", "expected", "work", "byte_count"),
    [
        pytest.param("compare", 1, 4, 0, id="limb-comparison"),
        pytest.param("zero", True, 1, 0, id="coefficient-zero"),
        pytest.param("negative", True, 1, 0, id="coefficient-sign"),
        pytest.param("negate", 1 << 30, 3, 4, id="unary-sign"),
        pytest.param("add", (1 << 30) + 1, 4, 4, id="addition"),
        pytest.param("subtract", (1 << 30) - 1, 4, 4, id="subtraction"),
        pytest.param("multiply", 10 << 30, 10, 5, id="multiplication"),
        pytest.param("divmod", (142, 6), 10, 3, id="quotient-and-remainder"),
        pytest.param("remainder", 6, 10, 3, id="remainder-full-reservation"),
        pytest.param("scale", 300, 12, 3, id="two-decimal-steps"),
        pytest.param("digits", 123, 10, 2, id="bounded-decimal-parse"),
        pytest.param("result", None, 4, 3, id="result-buffer"),
    ],
)
def test_structured_schema_integer_primitives_follow_the_normative_schedule(
    name: str, expected: object, work: int, byte_count: int
) -> None:
    meter = EvaluationMeter()

    actual = primitive(name, meter)

    assert actual == expected
    assert counters(meter) == (work, byte_count, 0)


@pytest.mark.parametrize(
    ("name", "work", "byte_count"),
    [
        ("negate", 3, 4),
        ("add", 4, 4),
        ("subtract", 4, 4),
        ("multiply", 10, 5),
        ("divmod", 10, 3),
        ("remainder", 10, 3),
        ("scale", 12, 3),
        ("digits", 10, 2),
        ("result", 4, 3),
    ],
)
@pytest.mark.parametrize("dimension", ["work", "bytes"])
@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
def test_structured_schema_integer_primitives_precharge_exact_owned_capacity(
    name: str, work: int, byte_count: int, dimension: Dimension, short: bool
) -> None:
    needed = work if dimension == "work" else byte_count
    meter = remaining_meter(dimension, needed - int(short))

    if short:
        with pytest.raises(EvaluationRefused) as rejected:
            primitive(name, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        assert meter.work <= PARAMETERS["max_evaluation_work"]
        assert meter.auxiliary_bytes <= PARAMETERS["max_auxiliary_bytes"]
        return
    primitive(name, meter)

    assert (meter.work if dimension == "work" else meter.auxiliary_bytes) == PARAMETERS[
        "max_evaluation_work" if dimension == "work" else "max_auxiliary_bytes"
    ]


@pytest.mark.parametrize("extra_bit", [False, True], ids=["inclusive", "one-over"])
@pytest.mark.parametrize("operation", ["multiply", "add", "scale", "divmod"])
def test_structured_schema_intermediate_bit_reservations_are_conservative(
    extra_bit: bool, operation: str
) -> None:
    meter = EvaluationMeter()
    left = 1 << (8191 + int(extra_bit))
    right = 1 << 8191
    if operation == "add":
        left = 1 << (16_382 + int(extra_bit))
        right = 1
    elif operation in {"scale", "divmod"}:
        left = 1 << (16_382 + int(extra_bit) + int(operation == "divmod"))
        right = left

    if extra_bit:
        with pytest.raises(EvaluationRefused) as rejected:
            intermediate(operation, left, right, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        assert meter.auxiliary_bytes == 0
        return
    result = intermediate(operation, left, right, meter)
    if operation == "multiply":
        expected: object = 1 << 16_382
    elif operation == "add":
        expected = left + 1
    elif operation == "scale":
        expected = left
    else:
        expected = (1, 0)

    assert result == expected
    assert meter.auxiliary_bytes == (
        0 if operation == "scale" else 4096 if operation == "divmod" else 2048
    )


@pytest.mark.parametrize(
    ("left", "right", "work"),
    [(0, 1 << 16_383, 1), (1 << 16_383, 0, 2)],
    ids=["left-zero", "right-zero"],
)
def test_structured_schema_zero_multiplication_reserves_no_magnitude(
    left: int, right: int, work: int
) -> None:
    meter = EvaluationMeter()

    result = meter.integer_multiply(left, right)

    assert result == 0
    assert counters(meter) == (work, 0, 0)


def test_structured_schema_zero_negation_reserves_no_magnitude() -> None:
    meter = EvaluationMeter()

    result = meter.integer_negate(0)

    assert result == 0
    assert counters(meter) == (1, 0, 0)


def test_structured_schema_failed_reservation_precedes_native_division() -> None:
    meter = EvaluationMeter(work=PARAMETERS["max_evaluation_work"])
    before = counters(meter)

    with pytest.raises(EvaluationRefused) as rejected:
        meter.integer_divmod(1, 0)

    assert rejected.value.reason == "evaluation_exhausted"
    assert counters(meter) == before


def test_structured_schema_evaluation_counters_are_shared_and_never_refunded() -> None:
    meter = remaining_meter("work", 8)

    addition = meter.integer_add(1, 2)
    multiplication = meter.integer_multiply(2, 3)
    exact = counters(meter)
    with pytest.raises(EvaluationRefused) as rejected:
        meter.integer_zero(0)

    assert addition == 3
    assert multiplication == 6
    assert exact == (PARAMETERS["max_evaluation_work"], 2, 0)
    assert rejected.value.reason == "evaluation_exhausted"
    assert counters(meter) == exact


def test_structured_schema_numeric_shortcut_schedule() -> None:
    meter = EvaluationMeter()
    number = -(1 << 13_999)

    comparison = numeric_compare(number, 5e-324, meter)
    before_zero = counters(meter)
    zero = meter.integer_scale(0, 1_000_000)
    zero_step = meter.integer_scale(number, 0)

    assert comparison == -1
    assert before_zero == (
        NATIVE_INTEGER_CONVERSION_WORK + MINIMUM_SUBNORMAL_CONVERSION_WORK + 9,
        24 + 24 + 1 + 2 + 2,
        1 + 2 + 1,
    )
    assert zero == 0
    assert zero_step is number
    assert counters(meter) == (before_zero[0] + 4, before_zero[1], before_zero[2])


@pytest.mark.parametrize("dimension", ["work", "bytes", "entries"])
@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
def test_structured_schema_float_conversion_precharges_all_owned_state(
    dimension: Dimension, short: bool
) -> None:
    required = {
        "work": DECIMAL_TENTHS_CONVERSION_WORK,
        "bytes": 24 + 24 + 2,
        "entries": 2 + 1,
    }
    meter = remaining_meter(dimension, required[dimension] - int(short))
    before = counters(meter)

    if short:
        with pytest.raises(EvaluationRefused) as rejected:
            decimal_number(0.3, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        assert meter.work <= PARAMETERS["max_evaluation_work"]
        assert meter.auxiliary_bytes <= PARAMETERS["max_auxiliary_bytes"]
        assert meter.auxiliary_entries <= PARAMETERS["max_auxiliary_entries"]
        return
    number = decimal_number(0.3, meter)

    assert (number.coefficient, number.exponent) == (3, -1)
    assert counters(meter) == (
        before[0] + required["work"],
        before[1] + required["bytes"],
        before[2] + required["entries"],
    )


def test_structured_schema_repeated_conversions_share_counters_without_a_cache() -> (
    None
):
    meter = remaining_meter("work", 2 * DECIMAL_TENTHS_CONVERSION_WORK)

    first = decimal_number(0.3, meter)
    second = decimal_number(0.3, meter)
    exact = counters(meter)
    with pytest.raises(EvaluationRefused) as rejected:
        decimal_number(0.3, meter)

    assert first == second
    assert first is not second
    assert exact == (PARAMETERS["max_evaluation_work"], 100, 6)
    assert rejected.value.reason == "evaluation_exhausted"
    assert counters(meter) == exact


@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
def test_structured_schema_conversion_record_entries_are_cumulative(
    short: bool,
) -> None:
    meter = remaining_meter("entries", 2 - int(short))

    first = decimal_number(1, meter)
    if short:
        with pytest.raises(EvaluationRefused) as rejected:
            decimal_number(2, meter)

        assert first.coefficient == 1
        assert rejected.value.reason == "evaluation_exhausted"
        assert meter.auxiliary_entries == PARAMETERS["max_auxiliary_entries"]
        return
    second = decimal_number(2, meter)

    assert (first.coefficient, second.coefficient) == (1, 2)
    assert meter.auxiliary_entries == PARAMETERS["max_auxiliary_entries"]
