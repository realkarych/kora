import json
import math
from collections.abc import Mapping

import pytest

from kora.cel import CheckedExpression, JsonValue, SchemaTypes, check
from kora.cel.types import (
    BOOL,
    DOUBLE,
    DYN,
    INT,
    STRING,
    CelType,
    ListOf,
    MapOf,
    Nullable,
    Record,
    RecordField,
)


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
    from kora.cel_eval.bounds import Size, measure
    from kora.cel_eval.convert import bind
    from kora.cel_eval.errors import ErrorKind

    checked = expression("number == number && padding == padding")
    values = {"number": 1, "padding": "x" * 4092}
    originals = [measure(value) for value in values.values()]

    result = bind(checked, values, {})

    assert all(isinstance(size, Size) for size in originals)
    assert sum(size.bytes for size in originals if isinstance(size, Size)) == 4095
    assert sum(size.values for size in originals if isinstance(size, Size)) == 2
    assert len(json.dumps(1.0)) + len(json.dumps(values["padding"])) == 4097
    assert result is ErrorKind.BOUND_EXCEEDED


def test_cel_binding_refuses_oversized_original_aggregate_before_numeric_shrink() -> (
    None
):
    from kora.cel_eval.bounds import Size, measure
    from kora.cel_eval.convert import bind
    from kora.cel_eval.errors import ErrorKind

    checked = expression("number == number && padding == padding")
    values = {"number": 1.0, "padding": "x" * 4092}
    originals = [measure(value) for value in values.values()]

    result = bind(checked, values, {"number": INT})

    assert all(isinstance(size, Size) for size in originals)
    assert sum(size.bytes for size in originals if isinstance(size, Size)) == 4097
    assert sum(size.values for size in originals if isinstance(size, Size)) == 2
    assert len(json.dumps(1)) + len(json.dumps(values["padding"])) == 4095
    assert result is ErrorKind.BOUND_EXCEEDED


@pytest.mark.parametrize(("number", "cel_type"), [(1, DOUBLE), (1.0, INT)])
def test_cel_binding_accepts_exact_original_or_converted_numeric_boundary(
    number: int | float, cel_type: CelType
) -> None:
    from kora.cel_eval.convert import BoundValues, bind

    checked = expression("number == number && padding == padding")
    values = {"number": number, "padding": "x" * 4091}

    result = bind(checked, values, {"number": cel_type})

    assert isinstance(result, BoundValues)
    assert result.bytes == (4096 if cel_type is DOUBLE else 4094)
    assert result.count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", ["bytes", "values"])
@pytest.mark.parametrize("offset", [0, 1])
async def test_cel_binding_aggregates_referenced_byte_and_value_limits(
    dimension: str, offset: int
) -> None:
    from kora.cel_eval.bounds import Size, measure
    from kora.cel_eval.convert import BoundValues, bind
    from kora.cel_eval.errors import ErrorContext, ErrorKind, EvaluationError
    from kora.cel_eval.thread import Evaluator
    from tests.cel_eval.test_cel_evaluation import clock

    checked = expression("left == left && right == right")
    values: dict[str, object] = (
        {"left": "x" * 2046, "right": "y" * (2046 + offset)}
        if dimension == "bytes"
        else {
            "left": [[] for _ in range(127)],
            "right": [[] for _ in range(127 + offset)],
        }
    )
    originals = [measure(value) for value in values.values()]
    evaluator = Evaluator(clock)

    bound = bind(checked, values, {})
    try:
        outcome = await evaluator.evaluate(checked, values, {}, ErrorContext("when"))
    finally:
        await evaluator.close()

    assert all(isinstance(size, Size) for size in originals)
    if offset > 0:
        assert bound is ErrorKind.BOUND_EXCEEDED
        assert isinstance(outcome, EvaluationError)
        assert outcome.kind is ErrorKind.BOUND_EXCEEDED
    else:
        assert isinstance(bound, BoundValues)
        assert (bound.bytes if dimension == "bytes" else bound.count) == (
            4096 if dimension == "bytes" else 256
        ) + offset
        assert outcome is True
    assert evaluator.snapshot().outstanding == evaluator.snapshot().handles == 0


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


def no_documents(uri: str) -> JsonValue | None:
    return None


def test_cel_converts_additional_properties_to_their_schema_type() -> None:
    from kora.cel_eval.convert import BoundValues, bind

    schema: JsonValue = {
        "type": "object",
        "properties": {"n": {"type": "number"}},
        "required": ["n"],
        "additionalProperties": {"type": "integer"},
    }
    record = SchemaTypes(no_documents).document(schema)
    checked = expression("inputs.m == inputs.m")
    types: Mapping[str, CelType] = {"inputs": Record({"m": RecordField(record, True)})}

    result = bind(checked, {"inputs": {"m": {"n": 1, "k": 2.0}}}, types)

    assert isinstance(result, BoundValues)
    converted = result.values["inputs"]
    assert converted == {"m": {"n": 1.0, "k": 2}}
    assert isinstance(converted, dict)
    member = converted["m"]
    assert isinstance(member, dict)
    assert (type(member["n"]), type(member["k"])) == (float, int)


KEYWORD_OBJECT: JsonValue = {"properties": {"a": {"type": "integer"}}}
KEYWORD_ARRAY: JsonValue = {"items": {"type": "integer"}}
MISMATCHED_KINDS: list[tuple[object, CelType]] = [
    ("1", INT),
    (True, DOUBLE),
    (1, STRING),
    ("true", BOOL),
    (None, STRING),
    ({}, ListOf(INT)),
    ([], Record({})),
    ([], MapOf(STRING, INT)),
    ("abc", SchemaTypes(no_documents).document(KEYWORD_OBJECT)),
    ({"kkk": 1}, SchemaTypes(no_documents).document(KEYWORD_ARRAY)),
    (["x"], ListOf(INT)),
]
MATCHING_KINDS: list[tuple[object, CelType]] = [
    (None, Nullable(STRING)),
    ("x", Nullable(STRING)),
    ([1, "x", None], DYN),
    ({"a": 1}, SchemaTypes(no_documents).document(KEYWORD_OBJECT)),
    ([1], SchemaTypes(no_documents).document(KEYWORD_ARRAY)),
    (False, BOOL),
]


@pytest.mark.parametrize(
    ("value", "cel_type", "refused"),
    [(value, cel_type, True) for value, cel_type in MISMATCHED_KINDS]
    + [(value, cel_type, False) for value, cel_type in MATCHING_KINDS],
)
def test_cel_conversion_refuses_values_of_another_json_kind(
    value: object, cel_type: CelType, refused: bool
) -> None:
    from kora.cel_eval.convert import bind
    from kora.cel_eval.errors import ErrorKind

    checked = expression("inputs.v == inputs.v")
    types: Mapping[str, CelType] = {
        "inputs": Record({"v": RecordField(cel_type, True)})
    }

    result = bind(checked, {"inputs": {"v": value}}, types)

    assert (result is ErrorKind.INVALID_VALUE) is refused


DECLARED_SCHEMA: JsonValue = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "maxItems": 2,
            "items": {"type": "string", "maxLength": 1},
        },
        "name": {"type": "string", "maxLength": 2},
    },
    "additionalProperties": {"type": "array", "maxItems": 0},
}
DECLARED_VALUES: list[tuple[str, JsonValue, bool]] = [
    ("within", {"tags": ["a", "\U0001f600"], "name": "ab", "extra": []}, True),
    ("too_many_items", {"tags": ["a", "b", "c"]}, False),
    ("within_utf8_bytes", {"tags": ["ab"], "name": "abcd"}, True),
    ("nested_string_too_long", {"tags": ["a", "abcde"]}, False),
    ("field_string_too_long", {"name": "abcdefghi"}, False),
    ("utf8_bytes_counted", {"name": "\U0001f600\U0001f600\U0001f600"}, False),
    ("additional_property", {"extra": [1]}, False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "admitted"),
    [(value, admitted) for _, value, admitted in DECLARED_VALUES],
    ids=[name for name, _, _ in DECLARED_VALUES],
)
async def test_cel_binding_refuses_data_over_declared_bounds(
    value: JsonValue, admitted: bool
) -> None:
    from kora.cel_eval.convert import BoundValues, bind
    from kora.cel_eval.errors import ErrorContext, ErrorKind, EvaluationError
    from kora.cel_eval.thread import Evaluator
    from tests.cel_eval.test_cel_evaluation import clock

    variable = SchemaTypes(no_documents).variable(DECLARED_SCHEMA)
    checked = expression("v == v")
    evaluator = Evaluator(clock)

    bound = bind(checked, {"v": value}, {"v": variable.type}, {"v": variable.bound})
    unchecked = bind(checked, {"v": value}, {"v": variable.type})
    try:
        outcome = await evaluator.evaluate(
            checked,
            {"v": value},
            {"v": variable.type},
            ErrorContext("when"),
            bounds={"v": variable.bound},
        )
    finally:
        await evaluator.close()

    assert isinstance(unchecked, BoundValues)
    if admitted:
        assert isinstance(bound, BoundValues)
        assert outcome is True
    else:
        assert bound is ErrorKind.BOUND_EXCEEDED
        assert isinstance(outcome, EvaluationError)
        assert outcome.kind is ErrorKind.BOUND_EXCEEDED
