from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Literal, cast

from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_keywords import (
    BOOLEAN_KEYWORDS,
    NUMBER_KEYWORDS,
    SIMPLE_TYPES,
    STRING_KEYWORDS,
)
from kora.ir.schema_locations import (
    FRAGMENT_CHARACTERS,
    HEX_DIGITS,
    LISTED_SCHEMAS,
    NAMED_SCHEMAS,
    SINGLE_SCHEMAS,
    Position,
    Schema,
    schema_child_position,
)
from kora.ir.standard_schema_policy import record, refuse, require
from kora.ir.structured_schema_locations import EXCLUDED, INERT, VOCABULARIES

EdgeKind = Literal["once", "array", "object"]


def decode_fragment(fragment: str) -> str:
    encoded = bytearray(len(fragment))
    cursor = written = 0
    while cursor < len(fragment):
        character = fragment[cursor]
        require(character in FRAGMENT_CHARACTERS)
        if character == "%":
            require(
                cursor + 2 < len(fragment)
                and fragment[cursor + 1] in HEX_DIGITS
                and fragment[cursor + 2] in HEX_DIGITS
            )
            encoded[written] = int(fragment[cursor + 1 : cursor + 3], 16)
            cursor += 3
        else:
            encoded[written] = ord(character)
            cursor += 1
        written += 1
    del encoded[written:]
    try:
        return encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        refuse()


def unique_strings(value: JsonValue) -> None:
    require(type(value) is list)
    values = cast("list[JsonValue]", value)
    require(all(type(item) is str for item in values))
    strings = cast("list[str]", values).copy()
    pending = [(0, len(strings), 0, 20)] if len(strings) > 1 else []
    while pending:
        start, end, character, bit = pending.pop()
        left, right = start, end
        while left < right:
            text = strings[left]
            point = ord(text[character]) + 1 if character < len(text) else 0
            if point & (1 << bit):
                right -= 1
                strings[left], strings[right] = strings[right], strings[left]
            else:
                left += 1
        for low, high in ((start, left), (left, end)):
            if high - low <= 1:
                continue
            if bit:
                pending.append((low, high, character, bit - 1))
            else:
                require(character < len(strings[low]))
                pending.append((low, high, character + 1, 20))


def arguments(schema: JsonObject, root: bool) -> None:
    for key, value in schema.items():
        require(key not in EXCLUDED and (root or key != "$id"))
        if key in STRING_KEYWORDS:
            require(type(value) is str)
            text = cast("str", value)
            if key == "$schema":
                require(text == "https://json-schema.org/draft/2020-12/schema")
            if key == "$id":
                require("#" not in text.rstrip("#") and text.count("#") <= 1)
            if key in {"$anchor", "$dynamicAnchor", "$recursiveAnchor"}:
                require(
                    bool(text)
                    and text[0]
                    in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_"
                )
                require(
                    all(
                        c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                        "abcdefghijklmnopqrstuvwxyz_0123456789-."
                        for c in text
                    )
                )
        elif key in NUMBER_KEYWORDS:
            require(type(value) is int or type(value) is float)
            number = cast("int | float", value)
            if key == "multipleOf":
                require(number > 0)
            elif key not in {
                "minimum",
                "maximum",
                "exclusiveMinimum",
                "exclusiveMaximum",
            }:
                require(
                    number >= 0 and (isinstance(number, int) or number.is_integer())
                )
        elif key in BOOLEAN_KEYWORDS:
            require(type(value) is bool)
        elif key == "type":
            if type(value) is str:
                require(value in SIMPLE_TYPES)
            else:
                unique_strings(value)
                require(
                    bool(value)
                    and all(item in SIMPLE_TYPES for item in cast("list[str]", value))
                )
        elif key == "required":
            unique_strings(value)
        elif key in NAMED_SCHEMAS | {
            "dependencies",
            "dependentRequired",
            "$vocabulary",
        }:
            require(type(value) is dict)
            for name, item in cast("JsonObject", value).items():
                if key == "$vocabulary":
                    require(type(item) is bool and (not item or name in VOCABULARIES))
                elif key == "dependentRequired" or (
                    key == "dependencies" and type(item) is list
                ):
                    unique_strings(item)
                else:
                    require(type(item) is dict or type(item) is bool)
        elif key in SINGLE_SCHEMAS:
            require(type(value) is dict or type(value) is bool)
        elif key in LISTED_SCHEMAS:
            require(type(value) is list and bool(value))
            require(
                all(
                    type(item) is dict or type(item) is bool
                    for item in cast("list[JsonValue]", value)
                )
            )
        elif key in {"enum", "examples"}:
            require(type(value) is list)


def children(schema: JsonObject) -> Iterator[tuple[str, str | int | None, Schema]]:
    for key, value in schema.items():
        if key in SINGLE_SCHEMAS:
            yield key, None, cast("Schema", value)
        elif key in NAMED_SCHEMAS | {"dependencies"}:
            for name, item in cast("JsonObject", value).items():
                if type(item) is not list:
                    yield key, name, cast("Schema", item)
        elif key in LISTED_SCHEMAS:
            for index, item in enumerate(cast("list[JsonValue]", value)):
                yield key, index, cast("Schema", item)


@dataclass(slots=True)
class Site:
    schema: Schema
    document: int
    parent: int
    keyword: str
    segment: str | int | None
    edges: list[tuple[int, EdgeKind]] = field(
        default_factory=list[tuple[int, EdgeKind]]
    )
    depth: int = 1
    remaining: int = 0
    calls: int = 1
    work: int = 0
    reachable: bool = False


class Sources:
    def __init__(self, source: object, limits: Mapping[str, int]) -> None:
        self.limits = limits
        self.sites: list[Site] = []
        self.index: dict[tuple[int, str, str | int | None], int] = {}
        self.references: dict[tuple[int, str], int] = {}
        self.documents: list[int] = []
        self.named: dict[str, int] = {}
        bundle = record(source, ("documents", "constraints", "metadata"))
        require(type(bundle["documents"]) is list)
        documents = cast("list[object]", bundle["documents"])
        require(bool(documents))
        require(len(documents) <= limits["max_schema_documents"], capacity=True)
        for document, raw in enumerate(documents):
            original = record(raw, ("name", "schema"))
            name = original["name"]
            require(name is None or type(name) is str)
            if type(name) is str:
                uri = "urn:kora:artifact:" + name.replace("/", ":")
                require(
                    uri not in self.named
                    and bool(name)
                    and ":" not in name
                    and "#" not in name
                )
                self.named[uri] = document
            self.documents.append(
                self.visit(original["schema"], document, -1, "", None)
            )
        constraints = bundle["constraints"]
        require(type(constraints) is list and bool(constraints))
        self.constraints = cast("list[int]", constraints)
        for constraint in cast("list[object]", constraints):
            require(type(constraint) is int)
            number = cast("int", constraint)
            require(0 <= number < len(self.documents))
        for current, site in enumerate(self.sites):
            if type(site.schema) is dict:
                for keyword in ("$ref", "$dynamicRef"):
                    if keyword in site.schema:
                        require(
                            len(self.references) < limits["max_reference_edges"],
                            capacity=True,
                        )
                        target = self.resolve(
                            cast("str", site.schema[keyword]), site.document
                        )
                        self.references[current, keyword] = target
                        site.edges.append((target, "once"))

    def roots(self) -> Iterator[int]:
        return (self.documents[index] for index in self.constraints)

    def visit(
        self,
        schema: object,
        document: int,
        parent: int,
        keyword: str,
        segment: str | int | None,
    ) -> int:
        require(type(schema) is dict or type(schema) is bool)
        require(len(self.sites) < self.limits["max_schema_positions"], capacity=True)
        original = cast("Schema", schema)
        current = len(self.sites)
        site = Site(original, document, parent, keyword, segment)
        self.sites.append(site)
        if parent >= 0:
            self.index[parent, keyword, segment] = current
        if type(original) is dict:
            arguments(original, parent < 0)
            for key, part, child in children(original):
                target = self.visit(child, document, current, key, part)
                category: EdgeKind = (
                    "array"
                    if key in {"items", "contains"}
                    else "object"
                    if key in {"additionalProperties", "propertyNames"}
                    else "once"
                )
                if key not in INERT:
                    site.edges.append((target, category))
        return current

    def resolve(self, reference: str, document: int) -> int:
        base, _, fragment = reference.partition("#")
        if base:
            require("/" not in base and base in self.named)
            document = self.named[base]
        decoded = decode_fragment(fragment)
        require(not decoded or decoded.startswith("/"))
        current = self.documents[document]
        value: JsonValue = self.sites[current].schema
        position, keyword = Position.SCHEMA, ""
        start = 1
        while start <= len(decoded) and decoded:
            end = decoded.find("/", start)
            if end < 0:
                end = len(decoded)
            token = decoded[start:end]
            require(
                all(
                    c != "~" or i + 1 < len(token) and token[i + 1] in "01"
                    for i, c in enumerate(token)
                )
            )
            key = token.replace("~1", "/").replace("~0", "~")
            segment: str | int = key
            if isinstance(value, dict):
                require(key in value)
                value = value[key]
            elif isinstance(value, list):
                require(
                    key.isascii() and key.isdigit() and len(key) <= len(str(len(value)))
                )
                require(key == "0" or not key.startswith("0"))
                segment = int(key)
                require(segment < len(value))
                value = value[segment]
            else:
                refuse()
            next_position = schema_child_position(
                position, key, isinstance(value, dict | bool)
            )
            require(next_position is not Position.DATA)
            if next_position is Position.SCHEMA:
                entry = (
                    (current, key, None)
                    if position is Position.SCHEMA
                    else (current, keyword, segment)
                )
                require(entry in self.index)
                current = self.index[entry]
            elif position is Position.SCHEMA:
                keyword = key
            position = next_position
            start = end + 1
        require(position is Position.SCHEMA)
        return current
