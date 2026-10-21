import math
from collections.abc import Mapping

import pytest

from kora.cel import CheckedExpression, check
from kora.cel.types import DOUBLE, DYN, INT, CelType, ListOf, Record, RecordField


def expression(source: str) -> CheckedExpression:
    checked = check(source)
    assert isinstance(checked, CheckedExpression)
    return checked


@pytest.mark.parametrize(
    ("value", "cel_type", "expected", "number_type"),
    [(100, DOUBLE, 100.0, float), (100, DYN, 100.0, float), (1.0, INT, 1, int)],
)
def test_cel_converts_json_numbers_to_schema_types(
    value: object, cel_type: CelType, expected: int | float, number_type: type
) -> None:
    from kora.cel_eval.convert import BoundValues, bind

    checked = expression("inputs.n == inputs.n")
    types = {"inputs": Record({"n": RecordField(cel_type, True)})}

    result = bind(checked, {"inputs": {"n": value}}, types)

    assert isinstance(result, BoundValues)
    assert result.values == {"inputs": {"n": expected}}
    stored = result.values["inputs"]
    assert isinstance(stored, dict)
    assert type(stored["n"]) is number_type


@pytest.mark.parametrize("value", [2**63, -(2**63) - 1, 1.5, math.inf, math.nan])
def test_cel_integer_conversion_refuses_out_of_range(value: object) -> None:
    from kora.cel_eval.convert import bind
    from kora.cel_eval.errors import ErrorKind

    checked = expression("n == n")

    result = bind(checked, {"n": value}, {"n": INT})

    assert result is ErrorKind.NUMBER_OUT_OF_RANGE


class TextSubclass(str):
    pass


@pytest.mark.parametrize(
    "value",
    ["\ud800", {1: "value"}, (1,), b"value", object(), TextSubclass("value")],
)
def test_cel_binding_refuses_unconvertible_values(value: object) -> None:
    from kora.cel_eval.convert import bind
    from kora.cel_eval.errors import ErrorKind

    checked = expression("payload == payload")

    result = bind(checked, {"payload": value}, {})

    assert result is ErrorKind.INVALID_VALUE


def nested(depth: int) -> object:
    value: object = None
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize(
    "value",
    ["x" * 16_385, [None] * 1_001, {str(n): None for n in range(1_001)}, nested(33)],
)
def test_cel_binding_refuses_over_bound_data(value: object) -> None:
    from kora.cel_eval.convert import bind
    from kora.cel_eval.errors import ErrorKind

    checked = expression("payload == payload")

    result = bind(checked, {"payload": value}, {})

    assert result is ErrorKind.BOUND_EXCEEDED


def test_cel_bounds_check_the_converted_representation() -> None:
    from kora.cel_eval.convert import bind
    from kora.cel_eval.errors import ErrorKind

    checked = expression("payload == payload")
    values = [[1] * 1_000 for _ in range(20)]

    result = bind(checked, {"payload": values}, {})

    assert result is ErrorKind.BOUND_EXCEEDED


def test_cel_binding_owns_only_referenced_detached_values() -> None:
    from kora.cel_eval.convert import BoundValues, bind

    checked = expression("inputs.n == inputs.n")
    original: dict[str, object] = {"n": [1]}
    types: Mapping[str, CelType] = {
        "inputs": Record({"n": RecordField(ListOf(INT), True)})
    }

    result = bind(checked, {"inputs": original, "unused": object()}, types)
    original["n"] = object()

    assert isinstance(result, BoundValues)
    assert result.values == {"inputs": {"n": [1]}}


def test_cel_binding_supplies_absent_outputs_and_refuses_unavailable_projection() -> (
    None
):
    from kora.cel_eval.convert import BoundValues, bind
    from kora.cel_eval.errors import ErrorKind

    outputs = expression("has(outputs.result)")
    inputs = expression("inputs == inputs")

    empty = bind(outputs, {}, {})
    missing = bind(inputs, {}, {})

    assert isinstance(empty, BoundValues)
    assert empty.values == {"outputs": {}}
    assert missing is ErrorKind.PROJECTION_UNAVAILABLE


def test_cel_binding_rejects_cycles_and_oversized_integers_before_encoding() -> None:
    from kora.cel_eval.convert import bind
    from kora.cel_eval.errors import ErrorKind

    checked = expression("payload == payload")
    cycle: list[object] = []
    cycle.append(cycle)

    cyclic = bind(checked, {"payload": cycle}, {})
    huge = bind(checked, {"payload": 1 << 100_000}, {})

    assert cyclic is ErrorKind.BOUND_EXCEEDED
    assert huge is ErrorKind.NUMBER_OUT_OF_RANGE
