from typing import cast

from kora.ir.structured_schema_cursors import (
    ArenaCursor,
    NativeCursor,
    NativeObjectIndex,
    array_item,
    container_count,
    cursor_kind,
    object_key,
    object_value,
    scalar_value,
)
from kora.ir.structured_schema_equality import json_equal
from kora.ir.structured_schema_evaluation_policy import refuse
from kora.ir.structured_schema_evaluation_work import EvaluationMeter


def _require_index(index: NativeObjectIndex, meter: EvaluationMeter) -> None:
    meter.reserve(2)
    if type(index) is not NativeObjectIndex:
        refuse()


def unique_items_matches(
    instance: NativeCursor, argument: ArenaCursor, meter: EvaluationMeter
) -> bool:
    kind = cursor_kind(instance, meter)
    meter.reserve()
    if kind != "array":
        return True
    argument_kind = cursor_kind(argument, meter)
    meter.reserve()
    if argument_kind != "boolean":
        refuse()
    enabled = scalar_value(argument, meter)
    meter.reserve()
    if enabled is False:
        return True
    count = container_count(instance, meter)
    meter.reserve()
    first = 0
    while True:
        meter.reserve()
        if first + 1 >= count:
            return True
        meter.reserve()
        second = first + 1
        while True:
            meter.reserve()
            if second == count:
                break
            left = cast("NativeCursor", array_item(instance, first, meter))
            right = cast("NativeCursor", array_item(instance, second, meter))
            same = json_equal(left, right, meter)
            meter.reserve()
            if same:
                return False
            meter.reserve()
            second += 1
        meter.reserve()
        first += 1


def required_names_present(
    index: NativeObjectIndex, argument: ArenaCursor, meter: EvaluationMeter
) -> bool:
    _require_index(index, meter)
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
            return True
        child = cast("ArenaCursor", array_item(argument, position, meter))
        child_kind = cursor_kind(child, meter)
        meter.reserve()
        if child_kind != "string":
            refuse()
        name = cast("str", scalar_value(child, meter))
        found = index.find(name, meter)
        meter.reserve()
        if found < 0:
            return False
        meter.reserve()
        position += 1


def dependent_required_present(
    index: NativeObjectIndex, argument: ArenaCursor, meter: EvaluationMeter
) -> bool:
    _require_index(index, meter)
    kind = cursor_kind(argument, meter)
    meter.reserve()
    if kind != "object":
        refuse()
    count = container_count(argument, meter)
    meter.reserve()
    position = 0
    while True:
        meter.reserve()
        if position == count:
            return True
        trigger = object_key(argument, None, position, meter)
        found = index.find(trigger, meter)
        meter.reserve()
        if found >= 0:
            required = cast(
                "ArenaCursor", object_value(argument, None, position, meter)
            )
            satisfied = required_names_present(index, required, meter)
            meter.reserve()
            if not satisfied:
                return False
        meter.reserve()
        position += 1
