import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from kora.ir.schema_locations import Position, schema_child_position
from kora.ir.standard_schema_policy import require


@dataclass(slots=True)
class SourceStatistics:
    nodes: int = 0
    encoded: int = 0
    depth: int = 0
    array: int = 0
    members: int = 0
    key_bytes: int = 0
    positions: int = 0
    references: int = 0


class SourceScan:
    def __init__(self, limits: Mapping[str, int]) -> None:
        self.limits = limits
        self.stats = SourceStatistics()
        self.ancestors: set[int] = set()

    def bound(self, name: str, value: int) -> None:
        require(value <= self.limits["max_schema_" + name], capacity=True)

    def add_bytes(self, count: int) -> None:
        self.stats.encoded += count
        self.bound("source_bytes", self.stats.encoded)

    def text(self, value: str) -> int:
        self.bound("string_bytes", len(value))
        utf8, encoded = 0, 2
        for character in value:
            point = ord(character)
            require(not 0xD800 <= point <= 0xDFFF)
            utf8 += (
                1 if point < 128 else 2 if point < 2048 else 3 if point < 65536 else 4
            )
            if character in '"\\\b\f\n\r\t':
                encoded += 2
            elif point < 32 or point >= 127:
                encoded += 6 if point < 65536 else 12
            else:
                encoded += 1
        self.bound("string_bytes", utf8)
        self.add_bytes(encoded)
        return utf8

    def scalar(self, value: object) -> None:
        if type(value) is str:
            self.text(value)
        elif value is None or type(value) is bool:
            self.add_bytes(4 if value is None or value is True else 5)
        elif type(value) is int:
            self.bound("integer_bits", value.bit_length())
            self.add_bytes(len(str(value)))
        elif type(value) is float:
            require(math.isfinite(value))
            self.add_bytes(len(repr(value)))
        else:
            require(False)

    def walk(
        self,
        value: object,
        depth: int = 0,
        wrapper: int = 0,
        position: Position = Position.DATA,
    ) -> None:
        self.stats.nodes += 1
        self.bound("nodes", self.stats.nodes)
        if position is Position.SCHEMA:
            self.stats.positions += 1
            self.bound("positions", self.stats.positions)
        kind = type(value)
        if kind is not list and kind is not dict:
            self.scalar(value)
            return
        depth += 1
        self.bound("depth", depth)
        identity = id(value)
        require(identity not in self.ancestors)
        self.ancestors.add(identity)
        count = len(cast("list[object] | dict[object, object]", value))
        self.bound("array_items" if kind is list else "object_members", count)
        self.bound("nodes", self.stats.nodes + count * (1 if kind is list else 2))
        self.stats.depth = max(self.stats.depth, depth)
        self.add_bytes(2 + max(0, count - 1))
        if kind is list:
            if wrapper == 2:
                self.bound("documents", count)
            self.stats.array = max(self.stats.array, count)
            for item in cast("list[object]", value):
                self.walk(
                    item,
                    depth,
                    3 if wrapper == 2 else 0,
                    Position.SCHEMA if position is Position.LISTED else Position.DATA,
                )
        else:
            self.stats.members = max(self.stats.members, count)
            for raw_key, item in cast("dict[object, object]", value).items():
                require(type(raw_key) is str)
                key = cast("str", raw_key)
                self.stats.nodes += 1
                self.bound("nodes", self.stats.nodes)
                self.stats.key_bytes = max(self.stats.key_bytes, self.text(key))
                self.add_bytes(1)
                reset = (wrapper == 3 and key == "schema") or (
                    wrapper == 1 and key == "metadata"
                )
                next_wrapper = 2 if wrapper == 1 and key == "documents" else 0
                semantic = schema_child_position(
                    position, key, type(item) is dict or type(item) is bool
                )
                if wrapper == 3 and key == "schema":
                    semantic = Position.SCHEMA
                if position is Position.SCHEMA and key in {"$ref", "$dynamicRef"}:
                    self.stats.references += 1
                    require(
                        self.stats.references <= self.limits["max_reference_edges"],
                        capacity=True,
                    )
                self.walk(item, 0 if reset else depth, next_wrapper, semantic)
        self.ancestors.remove(identity)

    def measure(self, source: object) -> SourceStatistics:
        self.walk(source, wrapper=1)
        return self.stats


def preparation_score(source: SourceStatistics, limit: int) -> int:
    score = source.nodes + source.references + source.positions + 1
    for factor in (source.members + 1, source.key_bytes + 1):
        score = min(limit + 1, score * factor)
    return score
