import math
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from kora.cel import CheckedExpression, JsonValue
from kora.cel.limits import MAX_INT, MIN_INT
from kora.cel.types import (
    DYN,
    CelType,
    Kind,
    Primitive,
    element_type,
    member_type,
    unwrap,
)
from kora.cel_eval.bounds import MAX_BYTES, MAX_VALUES, measure
from kora.cel_eval.errors import ErrorKind

MAX_DOUBLE_INTEGER: int = int(sys.float_info.max)
BUILTIN_TYPES: frozenset[str] = frozenset(
    {
        "int",
        "uint",
        "double",
        "bool",
        "string",
        "bytes",
        "list",
        "map",
        "null_type",
        "type",
    }
)


@dataclass(frozen=True)
class BoundValues:
    values: dict[str, JsonValue]
    bytes: int
    count: int


def number(value: int | float, cel_type: CelType) -> int | float | ErrorKind:
    inner = unwrap(cel_type)
    if isinstance(inner, Primitive) and inner.kind is Kind.INT:
        if value < MIN_INT or value > MAX_INT:
            return ErrorKind.NUMBER_OUT_OF_RANGE
        if type(value) is float and not value.is_integer():
            return ErrorKind.NUMBER_OUT_OF_RANGE
        return int(value)
    if abs(value) > MAX_DOUBLE_INTEGER:
        return ErrorKind.NUMBER_OUT_OF_RANGE
    converted = float(value)
    return converted if math.isfinite(converted) else ErrorKind.NUMBER_OUT_OF_RANGE


def convert(value: object, cel_type: CelType) -> JsonValue | ErrorKind:
    if type(value) in (int, float):
        return number(cast(int | float, value), cel_type)
    if type(value) is list:
        converted_list: list[JsonValue] = []
        child_type = element_type(cel_type)
        for child in cast(list[object], value):
            item = convert(child, child_type)
            if isinstance(item, ErrorKind):
                return item
            converted_list.append(item)
        return converted_list
    if type(value) is dict:
        converted_map: dict[str, JsonValue] = {}
        for key, child in cast(dict[str, object], value).items():
            item = convert(child, member_type(cel_type, key))
            if isinstance(item, ErrorKind):
                return item
            converted_map[key] = item
        return converted_map
    return cast(JsonValue, value)


def bind(
    checked: CheckedExpression,
    values: Mapping[str, object],
    types: Mapping[str, CelType],
) -> BoundValues | ErrorKind:
    result: dict[str, JsonValue] = {}
    total_bytes = 0
    total_values = 0
    original_bytes = 0
    original_values = 0
    for name in checked.variables:
        if name in BUILTIN_TYPES and name not in values:
            continue
        if name not in values and name != "outputs":
            return ErrorKind.PROJECTION_UNAVAILABLE
        value = values.get(name, {})
        before = measure(value)
        if isinstance(before, ErrorKind):
            return before
        original_bytes += before.bytes
        original_values += before.values
        if original_bytes > MAX_BYTES or original_values > MAX_VALUES:
            return ErrorKind.BOUND_EXCEEDED
        converted = convert(value, types.get(name, DYN))
        if isinstance(converted, ErrorKind):
            return converted
        after = measure(converted)
        if isinstance(after, ErrorKind):
            return after
        total_bytes += after.bytes
        total_values += after.values
        if total_bytes > MAX_BYTES or total_values > MAX_VALUES:
            return ErrorKind.BOUND_EXCEEDED
        result[name] = converted
    return BoundValues(result, total_bytes, total_values)
