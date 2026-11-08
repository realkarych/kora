import math
import sys
from traceback import format_exception, walk_tb
from typing import cast

import pytest

from kora.db.graph_value_json import ObjectReader, TextBudget, scalar_text
from kora.engine.graph_parameters import (
    build_graph_parameter_binding,
    read_graph_parameter_binding,
)
from kora.engine.parameter_schema_references import ParameterReferenceBudget
from kora.engine.parameter_schema_scalars import (
    ParameterScalarFailure,
    ParameterScalarRejected,
    prepare_parameter_schema_scalar,
)
from kora.ir.json import JsonObject, JsonValue
from tests.engine.test_graph_parameters import parameter_document

PRIVATE = "private-schema-scalar-marker"
MIXED = "A\x00\ud800😀"
NEGATIVE_MAX = -sys.float_info.max
LARGE = 10**4600 + 999_999_999


def ample() -> ParameterReferenceBudget:
    return ParameterReferenceBudget(2_000_000, 2_000_000, 10_000)


def saved_scalar(value: JsonValue) -> JsonValue:
    pinned = parameter_document({"const": value})
    text = build_graph_parameter_binding(pinned, max_encoded_bytes=20_000)
    saved = ObjectReader(text).object()
    checked = read_graph_parameter_binding(pinned, saved, max_encoded_bytes=20_000)
    return cast("JsonObject", ObjectReader(checked).object()["schema"])["const"]


@pytest.mark.parametrize(
    "value",
    [MIXED, 2**80, -(2**80), 0, -0.0, 1.0, NEGATIVE_MAX, True, False, None],
    ids=[
        "ascii-width",
        "wide-integer",
        "negative-wide-integer",
        "integer-zero",
        "negative-float-zero",
        "integral-float",
        "negative-maximum-float",
        "true",
        "false",
        "null",
    ],
)
def test_parameter_schema_scalar_preserves_checked_native_identity(
    value: JsonValue,
) -> None:
    exact = saved_scalar(value)
    encoded = scalar_text(exact, TextBudget(20_000))

    node = prepare_parameter_schema_scalar(exact, max_integer_bits=1000, budget=ample())

    assert node.kind == "scalar"
    assert node.value is exact
    assert type(node.value) is type(value)
    assert node.size == len(encoded)
    assert node.spelling == (
        encoded if type(value) is int or type(value) is float else ""
    )
    if type(value) is float and value == 0:
        assert type(node.value) is float
        assert math.copysign(1, node.value) == -1


def test_parameter_schema_scalar_spells_integers_beyond_the_native_digit_limit() -> (
    None
):
    value = saved_scalar(LARGE)
    encoded = scalar_text(value, TextBudget(20_000))
    assert type(value) is int

    node = prepare_parameter_schema_scalar(
        value, max_integer_bits=value.bit_length(), budget=ample()
    )

    assert node.value is value
    assert node.spelling == encoded
    assert node.size == 4601
    assert node.size > sys.int_info.default_max_str_digits
    assert node.spelling.endswith("999999999")


@pytest.mark.parametrize(
    ("value", "work", "payload", "entries"),
    [(MIXED, 6, 20, 1), (999_999_999, 40, 82, 6), (NEGATIVE_MAX, 4125, 106, 1)],
    ids=["string", "decimal-chunk", "float"],
)
def test_parameter_schema_scalar_accepts_exact_charges(
    value: JsonValue, work: int, payload: int, entries: int
) -> None:
    budget = ParameterReferenceBudget(work, payload, entries)
    encoded = scalar_text(value, TextBudget(100))

    result = prepare_parameter_schema_scalar(
        value, max_integer_bits=1000, budget=budget
    )

    assert result.size == len(encoded)
    assert budget == ParameterReferenceBudget(0, 0, 0)


@pytest.mark.parametrize(
    ("work", "payload", "entries"),
    [(39, 82, 6), (40, 81, 6), (40, 82, 5)],
    ids=["work", "payload", "entries"],
)
def test_parameter_schema_scalar_refuses_an_independently_short_integer_charge(
    work: int, payload: int, entries: int
) -> None:
    budget = ParameterReferenceBudget(work, payload, entries)

    with pytest.raises(ParameterScalarRejected) as refusal:
        prepare_parameter_schema_scalar(999_999_999, max_integer_bits=30, budget=budget)

    assert refusal.value.reason is ParameterScalarFailure.CAPACITY


def test_parameter_schema_scalar_refuses_short_float_storage_before_repr() -> None:
    budget = ParameterReferenceBudget(4125, 57, 1)

    with pytest.raises(ParameterScalarRejected) as refusal:
        prepare_parameter_schema_scalar(NEGATIVE_MAX, max_integer_bits=0, budget=budget)

    assert refusal.value.reason is ParameterScalarFailure.CAPACITY
    assert budget == ParameterReferenceBudget(4121, 47, 0)


def test_parameter_schema_scalar_refuses_bit_width_before_integer_spelling() -> None:
    value = -(2**80)
    budget = ParameterReferenceBudget(1000, 1000, 1000)

    with pytest.raises(ParameterScalarRejected) as refusal:
        prepare_parameter_schema_scalar(value, max_integer_bits=80, budget=budget)

    assert refusal.value.reason is ParameterScalarFailure.CAPACITY
    assert budget == ParameterReferenceBudget(998, 1000, 1000)


class ForeignInteger(int):
    def bit_length(self) -> int:
        raise RuntimeError(PRIVATE)


class ForeignBudget(ParameterReferenceBudget):
    def reserve(self, work: int = 1, byte_count: int = 0, entries: int = 0) -> None:
        raise RuntimeError(PRIVATE)


@pytest.mark.parametrize(
    ("value", "budget", "reason"),
    [
        ([PRIVATE], ParameterReferenceBudget(0, 0, 0), ParameterScalarFailure.INVALID),
        (ForeignInteger(1), ample(), ParameterScalarFailure.INVALID),
        (PRIVATE, ForeignBudget(1000, 1000, 1000), ParameterScalarFailure.INVALID),
        (math.inf, ParameterReferenceBudget(2, 0, 0), ParameterScalarFailure.NONFINITE),
        (math.nan, ample(), ParameterScalarFailure.NONFINITE),
        (PRIVATE, ParameterReferenceBudget(1, 0, 1), ParameterScalarFailure.CAPACITY),
    ],
    ids=[
        "container",
        "numeric-subclass",
        "budget-subclass",
        "infinite",
        "nan",
        "capacity",
    ],
)
def test_parameter_schema_scalar_refusals_retain_no_private_source(
    value: object, budget: ParameterReferenceBudget, reason: ParameterScalarFailure
) -> None:
    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        with pytest.raises(ParameterScalarRejected) as refusal:
            prepare_parameter_schema_scalar(value, max_integer_bits=1000, budget=budget)
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }

    assert refusal.value.reason is reason
    assert refusal.value.__context__ is refusal.value.__cause__ is None
    assert frames["prepare_parameter_schema_scalar"] == {"reason": reason}
    assert {"_prepare", "_integer", "_float", "charged_integer_text"}.isdisjoint(frames)
    assert PRIVATE not in "".join(format_exception(refusal.value))


def test_parameter_schema_scalar_refuses_an_incomplete_budget_privately() -> None:
    budget = ParameterReferenceBudget(100, 100, 100)
    del budget.work

    with pytest.raises(ParameterScalarRejected) as refusal:
        prepare_parameter_schema_scalar(PRIVATE, max_integer_bits=1000, budget=budget)
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }

    assert refusal.value.reason is ParameterScalarFailure.INVALID
    assert refusal.value.__context__ is refusal.value.__cause__ is None
    assert frames["prepare_parameter_schema_scalar"] == {
        "reason": ParameterScalarFailure.INVALID
    }
    assert {"_prepare", "valid_parameter_reference_budget"}.isdisjoint(frames)
    assert PRIVATE not in "".join(format_exception(refusal.value))


@pytest.mark.parametrize("max_bits", [True, -1, sys.maxsize + 1])
def test_parameter_schema_scalar_refuses_an_invalid_width_allowance(
    max_bits: int,
) -> None:
    budget = ParameterReferenceBudget(100, 100, 100)

    with pytest.raises(ParameterScalarRejected) as refusal:
        prepare_parameter_schema_scalar(1, max_integer_bits=max_bits, budget=budget)

    assert refusal.value.reason is ParameterScalarFailure.INVALID
    assert budget == ParameterReferenceBudget(100, 100, 100)
