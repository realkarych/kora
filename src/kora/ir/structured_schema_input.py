from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal, cast

from kora.ir.structured_schema_work import (
    PARAMETERS,
    PreparationMeter,
    merge_sort,
    refuse,
)

Scalar = None | bool | int | float | str


@dataclass(frozen=True, slots=True)
class SchemaNode:
    kind: Literal["scalar", "array", "object"]
    value: Scalar = field(default=None, repr=False)
    children: tuple[int, ...] = ()
    members: tuple[tuple[int, int], ...] = ()
    size: int = 0
    maximum_key: int = 0
    spelling: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class FrozenSchemaArena:
    nodes: tuple[SchemaNode, ...]
    root: int
    input_bytes: int
    input_depth: int

    def __repr__(self) -> str:
        return (
            f"FrozenSchemaArena(input_bytes={self.input_bytes}, "
            f"input_nodes={len(self.nodes)}, input_depth={self.input_depth})"
        )

    def encode(self, meter: PreparationMeter) -> bytes:
        return encode_arena(self.nodes, self.root, self.input_bytes, meter)


class SchemaArena:
    def __init__(self, meter: PreparationMeter) -> None:
        self.meter = meter
        self.nodes: list[SchemaNode] = []
        self.bytes = 0
        self.depth = 0
        self.sealed = False

    def at(self, index: int) -> SchemaNode:
        self.meter.reserve()
        return self.nodes[index]

    def capacity(self, nodes: int, size: int, depth: int) -> None:
        self.meter.reserve(3)
        if self.sealed:
            raise RuntimeError("schema arena is sealed")
        if (
            len(self.nodes) + nodes > PARAMETERS["max_input_nodes"]
            or self.bytes + size > PARAMETERS["max_input_bytes"]
            or depth > PARAMETERS["max_input_depth"]
        ):
            refuse("preparation_exhausted")

    def add(self, node: SchemaNode, depth: int = 0) -> int:
        self.capacity(1, node.size, depth)
        self.meter.reserve(3)
        self.bytes += node.size
        self.depth = max(self.depth, depth)
        self.nodes.append(node)
        return len(self.nodes) - 1

    def scalar(self, value: object) -> int:
        self.meter.reserve(2)
        kind = type(value)
        if value is not None and kind not in (bool, int, float, str):
            refuse()
        self.capacity(1, 1, 0)
        scalar = cast("Scalar", value)
        spelling = ""
        if type(scalar) is str:
            self.meter.reserve()
            self.capacity(1, len(scalar) + 2, 0)
            size = self.meter.string_size(scalar)
        elif type(scalar) is int:
            spelling = self.meter.integer(scalar)
            size = len(spelling)
        elif type(scalar) is float:
            spelling = self.meter.floating(scalar)
            size = len(spelling)
        else:
            size = 4 if scalar is None or scalar is True else 5
        self.capacity(1, size, 0)
        return self.add(SchemaNode("scalar", scalar, size=size, spelling=spelling))

    def container(
        self, kind: Literal["array", "object"], count: int, depth: int
    ) -> int:
        self.meter.reserve(2)
        minimum_nodes = 1 + count * (2 if kind == "object" else 1)
        minimum_bytes = 2 + max(0, count - 1) + count * (4 if kind == "object" else 1)
        self.capacity(minimum_nodes, minimum_bytes, depth)
        return self.add(
            SchemaNode(
                kind, size=2 + max(0, count - 1) + (count if kind == "object" else 0)
            ),
            depth,
        )

    def finish(
        self,
        index: int,
        children: tuple[int, ...] = (),
        members: tuple[tuple[int, int], ...] = (),
    ) -> int:
        self.meter.reserve()
        node = self.at(index)
        maximum = 0
        for key, _ in members:
            self.meter.reserve(3)
            maximum = max(maximum, len(cast("str", self.at(key).value)))
        self.nodes[index] = SchemaNode(
            node.kind,
            children=children,
            members=members,
            size=node.size,
            maximum_key=maximum,
        )
        return index

    def snapshot(self, value: object, depth: int = 1) -> int:
        if type(depth) is not int or depth < 1:
            raise ValueError("invalid schema arena depth")
        return self._snapshot(value, depth, [])

    def _snapshot(self, value: object, depth: int, active: list[int]) -> int:
        self.meter.reserve(2)
        kind = type(value)
        if value is None or kind in (bool, int, float, str):
            return self.scalar(value)
        if kind not in (list, dict):
            refuse()
        self.meter.reserve(1 + len(active))
        if id(value) in active:
            refuse()
        self.meter.reserve()
        count = len(cast("list[object] | dict[object, object]", value))
        index = self.container("array" if kind is list else "object", count, depth)
        self.meter.reserve(2, entries=2)
        active.append(id(value))
        if kind is list:
            children = tuple(
                self._children(cast("list[object]", value), depth + 1, active)
            )
            self.finish(index, children=children)
        else:
            mapping = cast("dict[object, object]", value)
            for key in mapping:
                self.meter.reserve(3)
                if type(key) is not str:
                    refuse()
            self.meter.reserve(entries=2 * count)
            rows = list(cast("dict[str, object]", mapping).items())
            merge_sort(rows, lambda row: row[0], self.meter)
            members = tuple(self._members(rows, depth + 1, active))
            self.finish(index, members=members)
        self.meter.reserve(2)
        active.pop()
        return index

    def _children(
        self, entries: list[object], depth: int, active: list[int]
    ) -> Iterator[int]:
        for item in entries:
            self.meter.reserve(3)
            yield self._snapshot(item, depth, active)

    def _members(
        self, rows: list[tuple[str, object]], depth: int, active: list[int]
    ) -> Iterator[tuple[int, int]]:
        for key, item in rows:
            self.meter.reserve(3)
            yield self.scalar(key), self._snapshot(item, depth, active)

    def text(self, index: int) -> str:
        value = self.at(index).value
        self.meter.reserve(2)
        if type(value) is not str:
            refuse()
        return value

    def lookup(self, index: int, query: str) -> int:
        node = self.at(index)
        if node.kind != "object":
            refuse()
        self.meter.reserve(
            1 + len(node.members).bit_length() * (1 + len(query) + node.maximum_key)
        )
        low, high = 0, len(node.members)
        while low < high:
            middle = (low + high) // 2
            if cast("str", self.nodes[node.members[middle][0]].value) < query:
                low = middle + 1
            else:
                high = middle
        return (
            node.members[low][1]
            if low < len(node.members)
            and self.nodes[node.members[low][0]].value == query
            else -1
        )

    def freeze(self, root: int) -> FrozenSchemaArena:
        self.meter.reserve(1 + 2 * len(self.nodes), entries=len(self.nodes))
        if self.sealed:
            raise RuntimeError("schema arena is sealed")
        result = FrozenSchemaArena(tuple(self.nodes), root, self.bytes, self.depth)
        self.nodes = []
        self.sealed = True
        return result

    def encode(self, root: int) -> bytes:
        return encode_arena(self.nodes, root, self.bytes, self.meter)


def encode_arena(
    nodes: tuple[SchemaNode, ...] | list[SchemaNode],
    root: int,
    size: int,
    meter: PreparationMeter,
) -> bytes:
    meter.result(size)
    output = bytearray(size)
    offset = 0

    def byte(value: int) -> None:
        nonlocal offset
        output[offset] = value
        offset += 1

    def emit(value: str) -> None:
        for character in value:
            point = ord(character)
            if point < 128:
                byte(point)
            elif point < 2048:
                byte(192 | point >> 6)
                byte(128 | point & 63)
            elif point < 65536:
                byte(224 | point >> 12)
                byte(128 | point >> 6 & 63)
                byte(128 | point & 63)
            else:
                byte(240 | point >> 18)
                byte(128 | point >> 12 & 63)
                byte(128 | point >> 6 & 63)
                byte(128 | point & 63)

    def quoted(value: str) -> None:
        byte(34)
        for character in value:
            point = ord(character)
            if point in (34, 92):
                byte(92)
                byte(point)
            elif point in (8, 9, 10, 12, 13):
                byte(92)
                byte(
                    98
                    if point == 8
                    else 116
                    if point == 9
                    else 110
                    if point == 10
                    else 102
                    if point == 12
                    else 114
                )
            elif point < 32:
                byte(92)
                byte(117)
                byte(48)
                byte(48)
                high, low = point >> 4, point & 15
                byte(48 + high if high < 10 else 87 + high)
                byte(48 + low if low < 10 else 87 + low)
            else:
                emit(character)
        byte(34)

    def write(index: int) -> None:
        meter.reserve(3)
        node = nodes[index]
        if node.kind == "scalar":
            if type(node.value) is str:
                quoted(node.value)
            else:
                emit(
                    node.spelling
                    or (
                        "null"
                        if node.value is None
                        else "true"
                        if node.value
                        else "false"
                    )
                )
        elif node.kind == "array":
            byte(91)
            for position, child in enumerate(node.children):
                meter.reserve(5)
                if position:
                    byte(44)
                write(child)
            byte(93)
        else:
            byte(123)
            for position, (key, child) in enumerate(node.members):
                meter.reserve(7)
                if position:
                    byte(44)
                write(key)
                byte(58)
                write(child)
            byte(125)

    write(root)
    if offset != size:
        raise RuntimeError("schema preparation encoding invariant")
    meter.result(size)
    return bytes(output)
