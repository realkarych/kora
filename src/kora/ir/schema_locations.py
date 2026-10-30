from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from urllib.parse import unquote

from kora.ir.json import JsonObject, JsonValue
from kora.ir.type_declarations import contract_path

Path = tuple[str | int, ...]
Schema = JsonObject | bool
SINGLE_SCHEMAS: frozenset[str] = frozenset(
    {
        "items",
        "additionalProperties",
        "unevaluatedItems",
        "unevaluatedProperties",
        "propertyNames",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    }
)
NAMED_SCHEMAS: frozenset[str] = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
LISTED_SCHEMAS: frozenset[str] = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
FRAGMENT_CHARACTERS: frozenset[str] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@/?%"
)
HEX_DIGITS: frozenset[str] = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True)
class Location:
    path: Path
    complete: bool = True

    def child(self, *segments: str | int) -> "Location":
        if not self.complete:
            return self
        if len(self.path) + len(segments) > 64:
            return Location(self.path, False)
        return Location(self.path + segments)


def contract_location(name: str) -> Location:
    path = contract_path(name, "schema")
    return Location(path, len(path) == 3)


def schema_children(
    schema: JsonObject, location: Location
) -> Iterator[tuple[Schema, Location]]:
    for keyword, value in schema.items():
        field = location.child(keyword)
        if keyword in SINGLE_SCHEMAS and isinstance(value, dict | bool):
            yield value, field
        elif keyword in NAMED_SCHEMAS | {"dependencies"} and isinstance(value, dict):
            for child in value.values():
                if isinstance(child, dict | bool):
                    yield child, field.child("<field>")
        elif keyword in LISTED_SCHEMAS and isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, dict | bool):
                    yield child, field.child(index)


def schema_sites(
    root: JsonObject, location: Location
) -> Iterator[tuple[Schema, Location]]:
    pending: list[Iterator[tuple[Schema, Location]]] = [iter(((root, location),))]
    while pending:
        site = next(pending[-1], None)
        if site is None:
            pending.pop()
            continue
        yield site
        schema, field = site
        if isinstance(schema, dict):
            pending.append(schema_children(schema, field))


class Position(Enum):
    SCHEMA = "schema"
    NAMED = "named"
    LISTED = "listed"
    DEPENDENCIES = "dependencies"
    DATA = "data"


def next_position(position: Position, key: str, value: JsonValue) -> Position:
    if position in {Position.NAMED, Position.LISTED}:
        return Position.SCHEMA
    if position is Position.DEPENDENCIES:
        return Position.SCHEMA if isinstance(value, dict | bool) else Position.DATA
    if position is Position.SCHEMA:
        if key in SINGLE_SCHEMAS:
            return Position.SCHEMA
        if key in NAMED_SCHEMAS:
            return Position.NAMED
        if key in LISTED_SCHEMAS:
            return Position.LISTED
        if key == "dependencies":
            return Position.DEPENDENCIES
    return Position.DATA


def decode_fragment(value: str) -> str | None:
    for index, character in enumerate(value):
        if character not in FRAGMENT_CHARACTERS:
            return None
        if character == "%" and (
            index + 2 >= len(value)
            or value[index + 1] not in HEX_DIGITS
            or value[index + 2] not in HEX_DIGITS
        ):
            return None
    try:
        return unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def pointer_resolves(root: JsonObject, fragment: str) -> bool:
    pointer = decode_fragment(fragment)
    if pointer is None or (pointer and not pointer.startswith("/")):
        return False
    value: JsonValue = root
    position = Position.SCHEMA
    for token in () if not pointer else pointer[1:].split("/"):
        if any(
            character == "~"
            and (index + 1 == len(token) or token[index + 1] not in "01")
            for index, character in enumerate(token)
        ):
            return False
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif (
            isinstance(value, list)
            and key.isascii()
            and key.isdigit()
            and len(key) <= len(str(len(value)))
            and (key == "0" or not key.startswith("0"))
            and int(key) < len(value)
        ):
            value = value[int(key)]
        else:
            return False
        position = next_position(position, key, value)
        if position is Position.SCHEMA and isinstance(value, dict) and "$id" in value:
            return False
    return position is Position.SCHEMA and isinstance(value, dict | bool)


class SavedReferences:
    def __init__(self, roots: dict[str, JsonObject]) -> None:
        self.roots = roots
        self.uris = {
            "urn:kora:artifact:" + name.replace("/", ":"): root
            for name, root in roots.items()
        }
        self.cache: dict[tuple[int, str], bool] = {}

    def resolves(self, root: JsonObject, reference: str) -> bool:
        identity = (id(root), reference)
        known = self.cache.get(identity)
        if known is not None:
            return known
        base, _, fragment = reference.partition("#")
        target = self.uris.get(base) if base else root
        result = target is not None and pointer_resolves(target, fragment)
        self.cache[identity] = result
        return result
