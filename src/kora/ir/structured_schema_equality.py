from dataclasses import dataclass
from typing import Literal, cast

from kora.ir.structured_schema_cursors import (
    ArenaCursor,
    Cursor,
    NativeCursor,
    NativeObjectIndex,
    array_item,
    build_native_object_index,
    container_count,
    cursor_kind,
    object_key,
    object_value,
    scalar_value,
    string_equal,
)
from kora.ir.structured_schema_evaluation_policy import refuse
from kora.ir.structured_schema_evaluation_work import EvaluationMeter
from kora.ir.structured_schema_numbers import numeric_equal

MAX_EQUALITY_FRAMES = 17
Phase = Literal["enter", "array", "keys", "values"]


@dataclass(slots=True, repr=False, eq=False)
class _PairFrame:
    left: Cursor
    right: Cursor
    phase: Phase = "enter"
    position: int = 0
    count: int = 0
    left_index: NativeObjectIndex | None = None
    right_index: NativeObjectIndex | None = None

    def __repr__(self) -> str:
        return "_PairFrame()"


def _push(
    stack: list[_PairFrame], left: Cursor, right: Cursor, meter: EvaluationMeter
) -> None:
    meter.reserve(2)
    if len(stack) + 1 > MAX_EQUALITY_FRAMES:
        refuse("evaluation_exhausted")
    meter.reserve(10, entries=2)
    stack.append(_PairFrame(left, right))


def _complete(stack: list[_PairFrame], meter: EvaluationMeter) -> None:
    stack.pop()
    meter.reserve()
    if stack:
        meter.reserve(4)
        parent = stack[-1]
        parent.position += 1


def _enter(frame: _PairFrame, meter: EvaluationMeter) -> bool | None:
    meter.reserve(2)
    left, right = frame.left, frame.right
    first = cursor_kind(left, meter)
    second = cursor_kind(right, meter)
    meter.reserve()
    if first != second:
        return False
    meter.reserve()
    if first == "number":
        return numeric_equal(
            scalar_value(left, meter), scalar_value(right, meter), meter
        )
    meter.reserve()
    if first == "null":
        return True
    meter.reserve()
    if first == "boolean":
        left_value = scalar_value(left, meter)
        right_value = scalar_value(right, meter)
        meter.reserve()
        return left_value is right_value
    meter.reserve()
    if first == "string":
        left_text = cast("str", scalar_value(left, meter))
        right_text = cast("str", scalar_value(right, meter))
        return string_equal(left_text, right_text, meter)
    left_count = container_count(left, meter)
    right_count = container_count(right, meter)
    meter.reserve()
    if left_count != right_count:
        return False
    meter.reserve()
    if not left_count:
        return True
    meter.reserve()
    if first == "array":
        meter.reserve(2)
        frame.phase, frame.count = "array", left_count
        return None
    meter.reserve(2)
    if type(left) is NativeCursor:
        index = build_native_object_index(
            cast("dict[str, object]", scalar_value(left, meter)), meter
        )
        meter.reserve()
        frame.left_index = index
    meter.reserve(2)
    if type(right) is NativeCursor:
        index = build_native_object_index(
            cast("dict[str, object]", scalar_value(right, meter)), meter
        )
        meter.reserve()
        frame.right_index = index
    meter.reserve(2)
    frame.phase, frame.count = "keys", left_count
    return None


def _keys(frame: _PairFrame, meter: EvaluationMeter) -> bool:
    meter.reserve(5)
    left, right, left_index, right_index, position = (
        frame.left,
        frame.right,
        frame.left_index,
        frame.right_index,
        frame.position,
    )
    first = object_key(left, left_index, position, meter)
    second = object_key(right, right_index, position, meter)
    equal = string_equal(first, second, meter)
    meter.reserve()
    if not equal:
        return False
    meter.reserve(2)
    frame.position = position + 1
    meter.reserve(2)
    if position + 1 == frame.count:
        meter.reserve(2)
        frame.phase, frame.position = "values", 0
    return True


def json_equal(
    left: NativeCursor, right: NativeCursor | ArenaCursor, meter: EvaluationMeter
) -> bool:
    meter.reserve(2, entries=1)
    stack: list[_PairFrame] = []
    try:
        _push(stack, left, right, meter)
        while True:
            meter.reserve()
            if not stack:
                return True
            meter.reserve(2)
            frame = stack[-1]
            phase = frame.phase
            meter.reserve()
            if phase == "enter":
                result = _enter(frame, meter)
                meter.reserve()
                if result is False:
                    return False
                meter.reserve()
                if result is True:
                    _complete(stack, meter)
                continue
            meter.reserve()
            if phase == "keys":
                equal = _keys(frame, meter)
                meter.reserve()
                if not equal:
                    return False
                continue
            meter.reserve(3)
            if frame.position == frame.count:
                _complete(stack, meter)
                continue
            meter.reserve(3)
            current_left, current_right, position = (
                frame.left,
                frame.right,
                frame.position,
            )
            meter.reserve()
            if phase == "array":
                first = array_item(current_left, position, meter)
                second = array_item(current_right, position, meter)
            else:
                meter.reserve(2)
                left_index, right_index = frame.left_index, frame.right_index
                first = object_value(current_left, left_index, position, meter)
                second = object_value(current_right, right_index, position, meter)
            _push(stack, first, second, meter)
    finally:
        stack.clear()
