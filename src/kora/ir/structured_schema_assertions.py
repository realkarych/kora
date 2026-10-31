from typing import Literal, cast

from kora.ir.structured_schema_cursors import (
    ArenaCursor,
    Kind,
    NativeCursor,
    array_item,
    container_count,
    cursor_kind,
    scalar_value,
    string_equal,
)
from kora.ir.structured_schema_equality import json_equal
from kora.ir.structured_schema_evaluation_policy import refuse
from kora.ir.structured_schema_evaluation_work import EvaluationMeter
from kora.ir.structured_schema_numbers import (
    numeric_compare,
    numeric_is_integer,
    numeric_multiple_of,
)

TypeName = Kind | Literal["integer"]


def _type_name(name: str, meter: EvaluationMeter) -> TypeName:
    same = string_equal(name, "array", meter)
    meter.reserve()
    if same:
        return "array"
    same = string_equal(name, "boolean", meter)
    meter.reserve()
    if same:
        return "boolean"
    same = string_equal(name, "integer", meter)
    meter.reserve()
    if same:
        return "integer"
    same = string_equal(name, "null", meter)
    meter.reserve()
    if same:
        return "null"
    same = string_equal(name, "number", meter)
    meter.reserve()
    if same:
        return "number"
    same = string_equal(name, "object", meter)
    meter.reserve()
    if same:
        return "object"
    same = string_equal(name, "string", meter)
    meter.reserve()
    if same:
        return "string"
    refuse()


def _matches_type(
    name: TypeName, kind: Kind, instance: NativeCursor, meter: EvaluationMeter
) -> bool:
    meter.reserve()
    if name == "integer":
        meter.reserve()
        if kind != "number":
            return False
        return numeric_is_integer(scalar_value(instance, meter), meter)
    meter.reserve()
    return kind == name


def type_matches(
    instance: NativeCursor, argument: ArenaCursor, meter: EvaluationMeter
) -> bool:
    kind = cursor_kind(instance, meter)
    argument_kind = cursor_kind(argument, meter)
    meter.reserve()
    if argument_kind == "string":
        name = cast("str", scalar_value(argument, meter))
        return _matches_type(_type_name(name, meter), kind, instance, meter)
    meter.reserve()
    if argument_kind != "array":
        refuse()
    count = container_count(argument, meter)
    meter.reserve()
    if count == 0:
        refuse()
    meter.reserve()
    position = 0
    while True:
        meter.reserve()
        if position == count:
            return False
        child = cast("ArenaCursor", array_item(argument, position, meter))
        child_kind = cursor_kind(child, meter)
        meter.reserve()
        if child_kind != "string":
            refuse()
        name = cast("str", scalar_value(child, meter))
        matches = _matches_type(_type_name(name, meter), kind, instance, meter)
        meter.reserve()
        if matches:
            return True
        meter.reserve()
        position += 1


def _bound_compare(
    instance: NativeCursor, argument: ArenaCursor, meter: EvaluationMeter
) -> Literal[-1, 0, 1] | None:
    kind = cursor_kind(instance, meter)
    meter.reserve()
    if kind != "number":
        return None
    argument_kind = cursor_kind(argument, meter)
    meter.reserve()
    if argument_kind != "number":
        refuse()
    left = scalar_value(instance, meter)
    right = scalar_value(argument, meter)
    return numeric_compare(left, right, meter)


def minimum_matches(
    instance: NativeCursor, argument: ArenaCursor, meter: EvaluationMeter
) -> bool:
    result = _bound_compare(instance, argument, meter)
    meter.reserve()
    if result is None:
        return True
    meter.reserve()
    return result >= 0


def maximum_matches(
    instance: NativeCursor, argument: ArenaCursor, meter: EvaluationMeter
) -> bool:
    result = _bound_compare(instance, argument, meter)
    meter.reserve()
    if result is None:
        return True
    meter.reserve()
    return result <= 0


def exclusive_minimum_matches(
    instance: NativeCursor, argument: ArenaCursor, meter: EvaluationMeter
) -> bool:
    result = _bound_compare(instance, argument, meter)
    meter.reserve()
    if result is None:
        return True
    meter.reserve()
    return result > 0


def exclusive_maximum_matches(
    instance: NativeCursor, argument: ArenaCursor, meter: EvaluationMeter
) -> bool:
    result = _bound_compare(instance, argument, meter)
    meter.reserve()
    if result is None:
        return True
    meter.reserve()
    return result < 0


def multiple_of_matches(
    instance: NativeCursor, argument: ArenaCursor, meter: EvaluationMeter
) -> bool:
    kind = cursor_kind(instance, meter)
    meter.reserve()
    if kind != "number":
        return True
    argument_kind = cursor_kind(argument, meter)
    meter.reserve()
    if argument_kind != "number":
        refuse()
    left = scalar_value(instance, meter)
    right = scalar_value(argument, meter)
    return numeric_multiple_of(left, right, meter)


def const_matches(
    instance: NativeCursor, argument: ArenaCursor, meter: EvaluationMeter
) -> bool:
    return json_equal(instance, argument, meter)


def enum_matches(
    instance: NativeCursor, argument: ArenaCursor, meter: EvaluationMeter
) -> bool:
    kind = cursor_kind(argument, meter)
    meter.reserve()
    if kind != "array":
        refuse()
    count = container_count(argument, meter)
    meter.reserve()
    position = 0
    while True:
        meter.reserve()
        if position == count:
            return False
        child = cast("ArenaCursor", array_item(argument, position, meter))
        same = json_equal(instance, child, meter)
        meter.reserve()
        if same:
            return True
        meter.reserve()
        position += 1
