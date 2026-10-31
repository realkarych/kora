from dataclasses import dataclass
from typing import Literal, cast

from kora.ir.structured_schema_evaluation_policy import refuse
from kora.ir.structured_schema_evaluation_work import EvaluationMeter
from kora.ir.structured_schema_input import SchemaNode

Kind = Literal["null", "boolean", "number", "string", "array", "object"]
Row = tuple[str, object]


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class NativeCursor:
    value: object

    def __repr__(self) -> str:
        return "NativeCursor()"


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ArenaCursor:
    nodes: tuple[SchemaNode, ...]
    node: int

    def __repr__(self) -> str:
        return "ArenaCursor()"


Cursor = NativeCursor | ArenaCursor


def native_cursor(value: object, meter: EvaluationMeter) -> NativeCursor:
    meter.reserve(2, entries=1)
    return NativeCursor(value)


def arena_cursor(
    nodes: tuple[SchemaNode, ...], node: int, meter: EvaluationMeter
) -> ArenaCursor:
    meter.reserve(3, entries=1)
    return ArenaCursor(nodes, node)


def _node(cursor: ArenaCursor, meter: EvaluationMeter) -> SchemaNode:
    meter.reserve(3)
    return cursor.nodes[cursor.node]


def _kind(value: object, meter: EvaluationMeter) -> Kind:
    meter.reserve()
    native_kind = type(value)
    meter.reserve()
    if value is None:
        return "null"
    meter.reserve()
    if native_kind is bool:
        return "boolean"
    meter.reserve()
    if native_kind is int:
        return "number"
    meter.reserve()
    if native_kind is float:
        return "number"
    meter.reserve()
    if native_kind is str:
        return "string"
    meter.reserve()
    if native_kind is list:
        return "array"
    meter.reserve()
    if native_kind is dict:
        return "object"
    refuse()


def cursor_kind(cursor: Cursor, meter: EvaluationMeter) -> Kind:
    meter.reserve(2)
    if type(cursor) is NativeCursor:
        meter.reserve()
        return _kind(cursor.value, meter)
    meter.reserve(2)
    if type(cursor) is not ArenaCursor:
        refuse()
    node = _node(cursor, meter)
    meter.reserve(2)
    if node.kind == "scalar":
        meter.reserve()
        return _kind(node.value, meter)
    meter.reserve(2)
    if node.kind == "array":
        return "array"
    meter.reserve(2)
    if node.kind == "object":
        return "object"
    refuse()


def scalar_value(cursor: Cursor, meter: EvaluationMeter) -> object:
    meter.reserve(2)
    if type(cursor) is NativeCursor:
        meter.reserve()
        return cursor.value
    node = _node(cast("ArenaCursor", cursor), meter)
    meter.reserve()
    return node.value


def container_count(cursor: Cursor, meter: EvaluationMeter) -> int:
    meter.reserve(2)
    if type(cursor) is NativeCursor:
        meter.reserve(2)
        return len(cast("list[object] | dict[str, object]", cursor.value))
    node = _node(cast("ArenaCursor", cursor), meter)
    meter.reserve(2)
    if node.kind == "array":
        meter.reserve(2)
        return len(node.children)
    meter.reserve(2)
    return len(node.members)


def array_item(cursor: Cursor, position: int, meter: EvaluationMeter) -> Cursor:
    meter.reserve(2)
    if type(cursor) is NativeCursor:
        meter.reserve(2)
        return native_cursor(cast("list[object]", cursor.value)[position], meter)
    node = _node(cast("ArenaCursor", cursor), meter)
    meter.reserve(3)
    return arena_cursor(
        cast("ArenaCursor", cursor).nodes, node.children[position], meter
    )


def string_equal(left: str, right: str, meter: EvaluationMeter) -> bool:
    meter.reserve(1 + min(len(left), len(right)))
    return left == right


@dataclass(slots=True, repr=False, eq=False)
class NativeObjectIndex:
    _rows: list[Row]
    maximum_key: int

    def __repr__(self) -> str:
        return "NativeObjectIndex()"

    def row(self, position: int, meter: EvaluationMeter) -> Row:
        meter.reserve(2)
        return self._rows[position]

    def find(self, query: str, meter: EvaluationMeter) -> int:
        meter.reserve(2)
        if type(query) is not str:
            refuse()
        meter.reserve(2)
        rows, maximum = self._rows, self.maximum_key
        count = len(rows)
        meter.reserve(
            1
            if not count
            else 1 + (count.bit_length() + 1) * (1 + len(query) + maximum)
        )
        low, high = 0, count
        while low < high:
            middle = (low + high) // 2
            if rows[middle][0] < query:
                low = middle + 1
            else:
                high = middle
        return low if low < count and rows[low][0] == query else -1


def _sort_rows(rows: list[Row], characters: int, meter: EvaluationMeter) -> None:
    count = len(rows)
    levels = (max(1, count) - 1).bit_length()
    meter.reserve(1, entries=1)
    meter.reserve(1 + 2 * levels * (count + characters), entries=2 * count)
    scratch = rows.copy()
    source, destination = rows, scratch
    width = 1
    while width < count:
        for start in range(0, count, 2 * width):
            left, middle, end = (
                start,
                min(start + width, count),
                min(start + 2 * width, count),
            )
            right = middle
            for position in range(start, end):
                if left < middle and (
                    right == end or source[left][0] <= source[right][0]
                ):
                    destination[position] = source[left]
                    left += 1
                else:
                    destination[position] = source[right]
                    right += 1
        source, destination = destination, source
        width *= 2
    if source is not rows:
        for position, row in enumerate(source):
            rows[position] = row


def build_native_object_index(
    mapping: dict[str, object], meter: EvaluationMeter
) -> NativeObjectIndex:
    meter.reserve(2)
    if type(mapping) is not dict:
        refuse()
    count = len(mapping)
    meter.reserve(1, entries=1)
    meter.reserve(1 + 3 * count, entries=2 * count)
    rows: list[Row] = []
    characters = 0
    maximum = 0
    for key, value in mapping.items():
        if type(key) is not str:
            refuse()
        length = len(key)
        characters += length
        maximum = max(maximum, length)
        rows.append((key, value))
    _sort_rows(rows, characters, meter)
    meter.reserve(3, entries=1)
    return NativeObjectIndex(rows, maximum)


def object_key(
    cursor: Cursor,
    index: NativeObjectIndex | None,
    position: int,
    meter: EvaluationMeter,
) -> str:
    meter.reserve(2)
    if type(cursor) is NativeCursor:
        meter.reserve()
        if index is None:
            raise RuntimeError("missing equality object index")
        row = index.row(position, meter)
        meter.reserve()
        return row[0]
    node = _node(cast("ArenaCursor", cursor), meter)
    meter.reserve(3)
    key = node.members[position][0]
    meter.reserve(3)
    return cast("str", cast("ArenaCursor", cursor).nodes[key].value)


def object_value(
    cursor: Cursor,
    index: NativeObjectIndex | None,
    position: int,
    meter: EvaluationMeter,
) -> Cursor:
    meter.reserve(2)
    if type(cursor) is NativeCursor:
        meter.reserve()
        if index is None:
            raise RuntimeError("missing equality object index")
        row = index.row(position, meter)
        meter.reserve()
        return native_cursor(row[1], meter)
    node = _node(cast("ArenaCursor", cursor), meter)
    meter.reserve(4)
    return arena_cursor(
        cast("ArenaCursor", cursor).nodes, node.members[position][1], meter
    )
