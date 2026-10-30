from collections.abc import Iterator

from kora.ir.structured_schema_input import SchemaArena
from kora.ir.structured_schema_work import (
    PARAMETERS,
    PreparationMeter,
    binary_lookup,
    merge_sort,
    refuse,
)

SINGLE = (
    "items",
    "additionalProperties",
    "propertyNames",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "contentSchema",
)
NAMED = ("properties", "$defs", "definitions", "dependentSchemas", "dependencies")
LISTED = ("allOf", "anyOf", "oneOf", "prefixItems")
INERT = ("$defs", "definitions", "contentSchema", "dependencies")
EXCLUDED = ("pattern", "patternProperties", "unevaluatedItems", "unevaluatedProperties")
VOCABULARIES = tuple(
    "https://json-schema.org/draft/2020-12/vocab/" + name
    for name in (
        "core",
        "applicator",
        "validation",
        "meta-data",
        "format-annotation",
        "content",
    )
)
FRAGMENT = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@/?%"
)


def matches(value: str, choices: tuple[str, ...], meter: PreparationMeter) -> bool:
    return any(meter.compare(value, choice) == 0 for choice in choices)


def decode_reference(
    reference: str, meter: PreparationMeter
) -> tuple[str | None, list[str], int]:
    length = len(reference)
    meter.reserve(1 + 8 * length, 4 * length, length + 1)
    pool = [""] * (length + 1)
    boundary = 0
    while boundary < length and reference[boundary] != "#":
        boundary += 1
    name: str | None = None
    if boundary:
        prefix = "urn:kora:artifact:"
        if boundary <= len(prefix) or not reference.startswith(prefix):
            refuse()
        name = "".join(
            "/" if reference[index] == ":" else reference[index]
            for index in range(len(prefix), boundary)
        )
    used, cursor, start = 0, 0, 0
    first, tilde = True, False
    remaining, point, minimum = 0, 0, 0
    index = boundary + 1
    while index < length:
        character = reference[index]
        if character not in FRAGMENT:
            refuse()
        byte = ord(character)
        index += 1
        if character == "%":
            if (
                index + 1 >= length
                or reference[index] not in "0123456789abcdefABCDEF"
                or reference[index + 1] not in "0123456789abcdefABCDEF"
            ):
                refuse()
            byte = int(reference[index] + reference[index + 1], 16)
            index += 2
        if remaining:
            if byte < 128 or byte > 191:
                refuse()
            point = point * 64 + (byte & 63)
            remaining -= 1
            if remaining:
                continue
            if point < minimum or point > 0x10FFFF or 0xD800 <= point <= 0xDFFF:
                refuse()
        elif byte < 128:
            point = byte
        elif 194 <= byte <= 244:
            remaining = 1 if byte < 224 else 2 if byte < 240 else 3
            minimum = 128 if remaining == 1 else 2048 if remaining == 2 else 65536
            point = byte & (31 if remaining == 1 else 15 if remaining == 2 else 7)
            continue
        else:
            refuse()
        decoded = chr(point)
        if first:
            if decoded != "/":
                refuse()
            first = False
            continue
        if tilde:
            if decoded not in "01":
                refuse()
            decoded = "~" if decoded == "0" else "/"
            tilde = False
        elif decoded == "~":
            tilde = True
            continue
        elif decoded == "/":
            pool[used] = "".join(pool[position] for position in range(start, cursor))
            used += 1
            cursor = start = used
            continue
        pool[cursor] = decoded
        cursor += 1
    if remaining or tilde:
        refuse()
    if not first:
        pool[used] = "".join(pool[position] for position in range(start, cursor))
        used += 1
    return name, pool, used


class SchemaLocations:
    def __init__(self, arena: SchemaArena) -> None:
        self.arena = arena
        self.meter = arena.meter
        self.positions: list[tuple[int, int, int, int, int]] = []
        self.references: list[tuple[int, int, int, int]] = []
        self.edges: list[tuple[int, int]] = []
        self.node_positions: tuple[tuple[int, int], ...] = ()

    def edge(self, source: int, target: int) -> None:
        self.meter.reserve(entries=1)
        self.edges.append((source, target))

    def enter(
        self,
        document: int,
        node: int,
        parent: int = -1,
        keyword: int = -1,
        segment: int = -1,
    ) -> int:
        self.meter.reserve(3)
        if len(self.positions) == PARAMETERS["max_schema_positions"]:
            refuse("preparation_exhausted")
        schema = self.arena.at(node)
        if schema.kind != "object" and type(schema.value) is not bool:
            refuse()
        self.meter.reserve(entries=2)
        position = len(self.positions)
        self.positions.append((document, node, parent, keyword, segment))
        if parent >= 0 and not matches(self.arena.text(keyword), INERT, self.meter):
            self.edge(parent, position)
        for key, value in schema.members:
            self.meter.reserve(3)
            name = self.arena.text(key)
            if matches(name, EXCLUDED, self.meter) or (
                parent >= 0 and self.meter.compare(name, "$id") == 0
            ):
                refuse()
            if (
                self.meter.compare(name, "$schema") == 0
                and self.meter.compare(
                    self.arena.text(value),
                    "https://json-schema.org/draft/2020-12/schema",
                )
                != 0
            ):
                refuse()
            if self.meter.compare(name, "$vocabulary") == 0:
                vocabulary = self.arena.at(value)
                if vocabulary.kind != "object":
                    refuse()
                for uri, required in vocabulary.members:
                    flag = self.arena.at(required).value
                    if type(flag) is not bool or (
                        flag
                        and not matches(self.arena.text(uri), VOCABULARIES, self.meter)
                    ):
                        refuse()
        for name in ("$dynamicRef", "$ref"):
            member = self.arena.member(node, name) if schema.kind == "object" else None
            if member is not None:
                key, value = member
                self.arena.text(value)
                if len(self.references) == PARAMETERS["max_reference_edges"]:
                    refuse("preparation_exhausted")
                self.meter.reserve(entries=1)
                self.references.append((position, key, value, -1))
        return position

    def children(self, node: int) -> Iterator[tuple[int, int, int]]:
        for key, value in self.arena.at(node).members:
            self.meter.reserve(3)
            name = self.arena.text(key)
            if matches(name, SINGLE, self.meter):
                yield value, key, -1
            elif matches(name, NAMED, self.meter):
                mapping = self.arena.at(value)
                if mapping.kind != "object":
                    refuse()
                for child_key, child in mapping.members:
                    if name == "dependencies" and self.arena.at(child).kind == "array":
                        continue
                    yield child, key, child_key
            elif matches(name, LISTED, self.meter):
                listing = self.arena.at(value)
                if listing.kind != "array":
                    refuse()
                for child_index, child in enumerate(listing.children):
                    yield child, key, -2 - child_index

    def visit(self, document: int, node: int) -> None:
        root = self.enter(document, node)
        stack = [(root, node, self.children(node))]
        while stack:
            self.meter.reserve(5)
            position, _, cursor = stack[-1]
            child = next(cursor, None)
            if child is None:
                stack.pop()
                continue
            value, key, segment = child
            current = self.enter(document, value, position, key, segment)
            stack.append((current, value, self.children(value)))

    def index(self) -> None:
        self.meter.reserve(entries=len(self.positions))

        def rows() -> Iterator[tuple[int, int]]:
            previous = -1
            for index, position in enumerate(self.positions):
                self.meter.reserve(5)
                node = position[1]
                if node <= previous:
                    raise RuntimeError("original schema node order invariant")
                previous = node
                yield node, index

        self.node_positions = tuple(rows())

    def path(self, position: int) -> int:
        def segments(current: int) -> Iterator[int]:
            self.meter.reserve(5)
            _, _, parent, keyword, segment = self.positions[current]
            if parent < 0:
                return
            yield from segments(parent)
            yield self.arena.scalar(self.arena.text(keyword))
            if segment >= 0:
                yield self.arena.scalar(self.arena.text(segment))
            elif segment < -1:
                yield self.arena.scalar(self.meter.integer(-2 - segment))

        return self.arena.wrap_array(tuple(segments(position)), 4)

    def target(self, root: int, pool: list[str], used: int) -> int:
        node = root
        for index in range(used):
            self.meter.reserve(3)
            token = pool[index]
            current = self.arena.at(node)
            if current.kind == "object":
                node = self.arena.lookup(node, token)
                if node < 0:
                    refuse()
            elif current.kind == "array":
                length, digits = len(current.children), 1
                while length >= 10:
                    self.meter.reserve(2)
                    length //= 10
                    digits += 1
                if not token or len(token) > digits:
                    refuse()
                self.meter.reserve(1 + len(token) ** 2, (4 * len(token) + 8) // 8)
                if any(character < "0" or character > "9" for character in token) or (
                    len(token) > 1 and token[0] == "0"
                ):
                    refuse()
                offset = int(token)
                if offset >= len(current.children):
                    refuse()
                node = current.children[offset]
            else:
                refuse()
        result = binary_lookup(self.node_positions, node, self.meter)
        if result < 0:
            refuse()
        return result

    def graph(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        merge_sort(self.edges, lambda row: row, self.meter)
        count = len(self.positions)
        self.meter.reserve(entries=count + 1 + len(self.edges))

        def starts() -> Iterator[int]:
            cursor = 0
            for position in range(count):
                self.meter.reserve(3)
                yield cursor
                while cursor < len(self.edges) and self.edges[cursor][0] == position:
                    self.meter.reserve(3)
                    cursor += 1
            self.meter.reserve()
            yield cursor

        def destinations() -> Iterator[int]:
            for _, target in self.edges:
                self.meter.reserve(3)
                yield target

        offsets = tuple(starts())
        targets = tuple(destinations())
        self.meter.reserve(entries=count)
        colors = [0] * count
        stack: list[tuple[int, int]] = []
        for position in range(count):
            if colors[position]:
                continue
            self.meter.reserve(3, entries=1)
            stack.append((position, offsets[position]))
            colors[position] = 1
            while stack:
                self.meter.reserve(7)
                source, next_edge = stack[-1]
                if next_edge == offsets[source + 1]:
                    colors[source] = 2
                    stack.pop()
                    continue
                target = targets[next_edge]
                stack[-1] = source, next_edge + 1
                if colors[target] == 1:
                    refuse()
                if colors[target] == 0:
                    self.meter.reserve(3, entries=1)
                    colors[target] = 1
                    stack.append((target, offsets[target]))
        return offsets, targets
