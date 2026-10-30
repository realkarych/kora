import pytest

from kora.ir.json import JsonValue
from kora.ir.schema_equality import compare_json
from tests.ir.output_schema_cases import deep_value

PAIRS: list[tuple[JsonValue, JsonValue, bool]] = [
    (None, None, True),
    (False, False, True),
    (True, True, True),
    (False, 0, False),
    (True, 1, False),
    (1, True, False),
    (False, None, False),
    (0, None, False),
    ("1", 1, False),
    (1, 1.0, True),
    (-0.0, 0, True),
    (0.0, -0.0, True),
    (1, 1.5, False),
    (2**53 + 1, float(2**53), False),
    (2**53, float(2**53), True),
    ("a", "b", False),
    ({}, {}, True),
    ({}, [], False),
    ([], {}, False),
    ({"x": []}, {"x": {}}, False),
    ({"a": 1, "b": [True, None]}, {"b": [True, None], "a": 1.0}, True),
    ({"a": 1}, {"b": 1}, False),
    ({"a": 1}, {}, False),
    ([1, 2], [2, 1], False),
    ([1], [1, 2], False),
    ({"const": True}, {"const": 1}, False),
    ({"required": ["a", "b"]}, {"required": ["b", "a"]}, False),
    ({"description": "a"}, {"description": "b"}, False),
    ({"$ref": "#/$defs/a"}, {"$ref": "#/$defs/b"}, False),
    (
        {"type": "string"},
        {"type": "string", "$schema": "https://json-schema.org/draft/2020-12/schema"},
        False,
    ),
]


@pytest.mark.parametrize(("left", "right", "equal"), PAIRS)
def test_schema_equality_uses_structural_json_values(
    left: JsonValue, right: JsonValue, equal: bool
) -> None:
    result = compare_json(left, right)

    assert result.equal is equal
    assert result.values_checked >= 1


@pytest.mark.parametrize("equal", [False, True])
def test_schema_equality_compares_deep_values_without_recursion(equal: bool) -> None:
    left = deep_value(5000)
    right = deep_value(5000, 0 if equal else 1)

    result = compare_json(left, right)

    assert result.equal is equal
    assert result.values_checked == 5001


def test_schema_equality_preserves_huge_integer_precision() -> None:
    left = 10**10000

    same = compare_json(left, left)
    different = compare_json(left, left + 1)
    floating = compare_json(left, 1e308)

    assert same.equal
    assert not different.equal
    assert not floating.equal


@pytest.mark.parametrize("early_mismatch", [False, True])
def test_schema_equality_visits_wide_object_values_lazily(early_mismatch: bool) -> None:
    left: JsonValue = {f"k{index}": index for index in range(10000)}
    right: JsonValue = {f"k{index}": index for index in reversed(range(10000))}
    if early_mismatch:
        right["k0"] = -1

    result = compare_json(left, right)

    assert result.equal is not early_mismatch
    assert result.values_checked == (2 if early_mismatch else 10001)


def test_schema_equality_keeps_each_parent_object_iterator_bound() -> None:
    left: JsonValue = {"first": {"nested": [1, {"leaf": 2}]}, "second": {"leaf": 3}}
    right: JsonValue = {"second": {"leaf": 3}, "first": {"nested": [1, {"leaf": 2}]}}

    result = compare_json(left, right)

    assert result.equal
    assert result.values_checked == 8
