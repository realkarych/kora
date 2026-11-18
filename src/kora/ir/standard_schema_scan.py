import math
from collections.abc import Mapping
from typing import cast

from kora.ir.schema_locations import Position, schema_child_position
from kora.ir.standard_schema_policy import SchemaDomain, require
from kora.ir.standard_schema_values import (
    Bounds,
    Statistics,
    integer_repr,
    numeric_score,
)


class SourceScan:
    def __init__(
        self,
        limits: Mapping[str, int],
        domain: SchemaDomain = SchemaDomain.OUTPUT,
        schema: bool = True,
    ) -> None:
        self.limits = limits
        self.domain = domain
        self.schema = schema
        self.prefix = "max_schema_" if schema else "max_value_"
        self.stats = Statistics()
        self.nodes = 0
        self.width = 0
        self.ancestors: set[int] = set()

    def bound(self, name: str, value: int) -> None:
        require(value <= self.limits[self.prefix + name], capacity=True)

    def text(self, value: str) -> tuple[int, int]:
        self.bound("string_bytes", len(value))
        utf8, encoded = 0, 2
        for character in value:
            point = ord(character)
            require(not 0xD800 <= point <= 0xDFFF)
            width = (
                1 if point < 128 else 2 if point < 2048 else 3 if point < 65536 else 4
            )
            utf8 += width
            if character in '"\\\b\f\n\r\t':
                encoded += 2
            elif point < 32 or (self.schema and point >= 127):
                encoded += 6 if point < 65536 else 12
            else:
                encoded += width
        self.bound("string_bytes", utf8)
        self.stats.representations += 12 * len(value) + 2
        self.stats.owned_bytes += 8 * len(value)
        return utf8, encoded

    def scalar(self, value: object) -> Bounds:
        result = Bounds(nodes=1)
        if type(value) is str:
            _, result.encoded = self.text(value)
        elif value is None or type(value) is bool:
            result.encoded = 4 if value is None or value is True else 5
            self.stats.representations += 5
        elif type(value) is int or type(value) is float:
            if type(value) is int:
                self.bound("integer_bits", value.bit_length())
                if not self.schema and self.domain is SchemaDomain.PARAMETERS:
                    require(-(2**63) <= value < 2**63)
                spelling = str(value)
                result.bits = value.bit_length()
                self.stats.representations += integer_repr(result.bits)
                self.stats.owned_bytes += 2 * ((result.bits + 7) // 8 + 32)
            else:
                require(math.isfinite(value))
                spelling = repr(value)
                mantissa, _, exponent = spelling.partition("e")
                result.bits = (
                    4 * (sum(c.isdigit() for c in mantissa) + abs(int(exponent or "0")))
                    + 4
                )
                self.stats.conversions += numeric_score(result.bits)
                self.stats.representations += 2 * integer_repr(result.bits) + 16
                self.stats.owned_bytes += 8 + 2 * ((result.bits + 7) // 8 + 32)
            require(
                2 * result.bits + 2 <= self.limits["max_numeric_intermediate_bits"],
                capacity=True,
            )
            result.numeric = True
            result.encoded = len(spelling)
        else:
            require(False)
        return result

    def walk(
        self,
        value: object,
        depth: int = 0,
        source_position: int = 0,
        position: Position = Position.DATA,
    ) -> Bounds:
        self.nodes += 1
        self.bound("nodes", self.nodes)
        if position is Position.SCHEMA:
            self.stats.positions += 1
            self.bound("positions", self.stats.positions)
        kind = type(value)
        if kind is not list and kind is not dict:
            result = self.scalar(value)
            self.width += result.encoded
            self.bound("source_bytes" if self.schema else "canonical_bytes", self.width)
            return result
        depth += 1
        allowed = self.limits[self.prefix + "depth"]
        if not self.schema and self.domain is SchemaDomain.PARAMETERS:
            allowed += 1
        require(depth <= allowed, capacity=True)
        identity = id(value)
        require(identity not in self.ancestors)
        self.ancestors.add(identity)
        count = len(cast("list[object] | dict[object, object]", value))
        self.bound("array_items" if kind is list else "object_members", count)
        self.bound("nodes", self.nodes + count * (1 if kind is list else 2))
        self.stats.depth = max(self.stats.depth, depth)
        result = Bounds(nodes=1, encoded=2 + max(0, count - 1))
        self.width += result.encoded
        self.stats.representations += 2 + 4 * count
        if kind is list:
            if source_position == 2:
                self.bound("documents", count)
            self.stats.array = max(self.stats.array, count)
            for item in cast("list[object]", value):
                child = self.walk(
                    item,
                    depth,
                    3 if source_position == 2 else 0,
                    Position.SCHEMA if position is Position.LISTED else Position.DATA,
                )
                self.stats.element.maximum(child)
                result.include(child)
        else:
            self.stats.members = max(self.stats.members, count)
            for raw_key, item in cast("dict[object, object]", value).items():
                require(type(raw_key) is str)
                key = cast("str", raw_key)
                self.nodes += 1
                self.bound("nodes", self.nodes)
                key_bytes, encoded = self.text(key)
                self.width += encoded + 1
                result.nodes += 1
                result.encoded += encoded + 1
                result.key_bytes = max(result.key_bytes, key_bytes)
                reset = (source_position == 3 and key == "schema") or (
                    source_position == 1 and key == "metadata"
                )
                child_position = 2 if source_position == 1 and key == "documents" else 0
                semantic = schema_child_position(
                    position, key, type(item) is dict or type(item) is bool
                )
                if source_position == 3 and key == "schema":
                    semantic = Position.SCHEMA
                if position is Position.SCHEMA and key in {"$ref", "$dynamicRef"}:
                    self.stats.references += 1
                    require(
                        self.stats.references <= self.limits["max_reference_edges"],
                        capacity=True,
                    )
                result.include(
                    self.walk(item, 0 if reset else depth, child_position, semantic)
                )
        self.ancestors.remove(identity)
        self.bound("source_bytes" if self.schema else "canonical_bytes", self.width)
        return result

    def measure(self, value: object, *, complete_source: bool = True) -> Statistics:
        self.stats.tree = self.walk(
            value, source_position=int(complete_source and self.schema)
        )
        return self.stats


def preparation_score(source: Statistics, limit: int) -> int:
    score = source.tree.nodes + source.references + source.positions + 1
    for factor in (source.members + 1, source.tree.key_bytes + 1):
        score = min(limit + 1, score * factor)
    return score
