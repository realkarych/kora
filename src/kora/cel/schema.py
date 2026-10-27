from collections.abc import Callable, Generator
from enum import Enum
from typing import Final, cast
from urllib.parse import unquote

from kora.cel.meter import Meter
from kora.cel.paths import JsonValue
from kora.cel.types import (
    BOOL,
    DOUBLE,
    DYN,
    INT,
    NULL,
    STRING,
    CelType,
    Kind,
    ListOf,
    MapOf,
    Primitive,
    Record,
    RecordField,
    nullable,
)

JsonObject = dict[str, JsonValue]
Resolver = Callable[[str], JsonValue | None]
Located = tuple[JsonValue, JsonValue]
MemoKey = tuple[int, int, "SchemaView"]
TargetKey = tuple[int, int]
Mapping = Generator[Located, CelType, CelType]

NULL_TYPE: Final[str] = "null"
SCALAR_TYPES: Final[dict[str, CelType]] = {
    "integer": INT,
    "number": DOUBLE,
    "boolean": BOOL,
}


class SchemaView(Enum):
    PLAIN = "kora.execution.v1"
    STRUCTURED_OUTPUT = "kora.structured-output-view.v1"


class SchemaError(ValueError):
    pass


class Position(Enum):
    SCHEMA = "schema"
    NAMED_SCHEMAS = "named_schemas"
    LISTED_SCHEMAS = "listed_schemas"
    DATA = "data"


SCHEMA_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"items", "additionalProperties", "not", "if", "then", "else", "contains"}
    | {"propertyNames", "unevaluatedItems", "unevaluatedProperties"}
)
NAMED_SCHEMA_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
LISTED_SCHEMA_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"allOf", "anyOf", "oneOf", "prefixItems"}
)


def next_position(position: Position, name: str) -> Position:
    if position in (Position.NAMED_SCHEMAS, Position.LISTED_SCHEMAS):
        return Position.SCHEMA
    if position is Position.DATA:
        return Position.DATA
    if name in NAMED_SCHEMA_KEYWORDS:
        return Position.NAMED_SCHEMAS
    if name in LISTED_SCHEMA_KEYWORDS:
        return Position.LISTED_SCHEMAS
    return Position.SCHEMA if name in SCHEMA_KEYWORDS else Position.DATA


def json_type(value: JsonValue) -> str:
    if value is None:
        return NULL_TYPE
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def entry(schema: JsonObject, name: str, kind: type | tuple[type, ...]) -> object:
    if name not in schema:
        return None
    value = schema[name]
    if not isinstance(value, kind):
        raise SchemaError("a schema keyword has the wrong JSON type")
    return value


def enumerated(schema: JsonObject) -> list[JsonValue] | None:
    values = cast("list[JsonValue] | None", entry(schema, "enum", list))
    return [schema["const"]] if "const" in schema else values


def is_schema(value: JsonValue) -> bool:
    return isinstance(value, dict | bool)


def is_position(name: str, length: int) -> bool:
    digits = name.isascii() and name.isdigit() and len(name) <= len(str(length))
    canonical = name == "0" or not name.startswith("0")
    return digits and canonical and int(name) < length


def is_null_schema(schema: JsonObject) -> bool:
    declared = schema.get("type")
    if declared == NULL_TYPE or declared == [NULL_TYPE]:
        return True
    if "const" in schema:
        return schema["const"] is None
    return schema.get("enum") == [None]


def strings(values: list[JsonValue]) -> list[str]:
    names = [name for name in values if isinstance(name, str)]
    if len(names) != len(values):
        raise SchemaError("a schema keyword has the wrong JSON type")
    return names


def check_keywords(schema: JsonObject) -> None:
    entry(schema, "properties", dict)
    entry(schema, "additionalProperties", (dict, bool))
    patterns = cast(
        "dict[str, JsonValue]", entry(schema, "patternProperties", dict) or {}
    )
    if not all(isinstance(pattern, dict | bool) for pattern in patterns.values()):
        raise SchemaError("a schema keyword has the wrong JSON type")
    entry(schema, "items", (dict, bool))
    entry(schema, "prefixItems", list)
    entry(schema, "anyOf", list)
    entry(schema, "oneOf", list)
    entry(schema, "type", (str, list))


def keyword_kind(schema: JsonObject) -> str | None:
    if "properties" in schema:
        return "object"
    if "items" in schema or "prefixItems" in schema:
        return "array"
    return None


class SchemaTypes:
    def __init__(self, resolver: Resolver, meter: Meter | None = None) -> None:
        self.resolver: Resolver = resolver
        self.meter: Meter = meter if meter is not None else Meter()
        self.memo: dict[MemoKey, CelType] = {}
        self.active: set[MemoKey] = set()
        self.documents: dict[str, JsonValue] = {}
        self.references: dict[tuple[int, str], Located] = {}
        self.targets: dict[TargetKey, Located | None] = {}
        self.held: list[JsonValue] = []
        self.written: list[MemoKey] = []

    def document(
        self, schema: JsonValue, view: SchemaView = SchemaView.PLAIN
    ) -> CelType:
        return self.mapped(schema, schema, view)

    def reference(self, uri: str, view: SchemaView = SchemaView.PLAIN) -> CelType:
        schema, root = self.resolve(None, uri)
        return self.mapped(schema, root, view)

    def resolve(self, root: JsonValue, uri: str) -> Located:
        cache_key = (id(root), uri)
        known = self.references.get(cache_key)
        if known is not None:
            return known
        base, _, fragment = uri.partition("#")
        if base:
            target = self.load(base)
        elif root is None:
            raise SchemaError("a local reference has no document")
        else:
            target = root
        located = (self.pointer(target, unquote(fragment)), target)
        self.held.append(target)
        self.references[cache_key] = located
        return located

    def load(self, base: str) -> JsonValue:
        known = self.documents.get(base)
        if known is not None:
            return known
        document = self.resolver(base)
        if document is None:
            raise SchemaError("a schema reference does not resolve")
        self.held.append(document)
        self.documents[base] = document
        return document

    def pointer(self, document: JsonValue, fragment: str) -> JsonValue:
        if not fragment:
            return document
        if not fragment.startswith("/"):
            raise SchemaError("a schema reference is not a JSON pointer")
        node = document
        position = Position.SCHEMA
        for token in fragment[1:].split("/"):
            self.meter.tick()
            name = token.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and name in node:
                node = node[name]
            elif isinstance(node, list) and is_position(name, len(node)):
                node = node[int(name)]
            else:
                raise SchemaError("a schema reference does not resolve")
            position = next_position(position, name)
            if position is Position.SCHEMA and isinstance(node, dict) and "$id" in node:
                raise SchemaError("a nested schema resource is not supported")
        return node

    def target(self, schema: JsonValue, root: JsonValue) -> Located | None:
        chain: list[Located] = []
        seen: set[int] = set()
        located: Located | None = None
        while isinstance(schema, dict):
            if (id(schema), id(root)) in self.targets:
                located = self.targets[(id(schema), id(root))]
                break
            self.meter.tick()
            if id(schema) in seen:
                break
            seen.add(id(schema))
            chain.append((schema, root))
            if "$id" in schema and schema is not root:
                raise SchemaError("a nested schema resource is not supported")
            reference = entry(schema, "$ref", str)
            parts = cast("list[JsonValue] | None", entry(schema, "allOf", list))
            if isinstance(reference, str):
                schema, root = self.resolve(root, reference)
            elif parts is not None and len(parts) == 1:
                schema = parts[0]
            else:
                located = (schema, root)
                break
        else:
            if not is_schema(schema):
                raise SchemaError("a schema is not an object or a boolean")
            located = (schema, root)
        for link, link_root in chain:
            self.targets[(id(link), id(link_root))] = located
            self.held.append(link)
        return located

    def mapped(self, schema: JsonValue, root: JsonValue, view: SchemaView) -> CelType:
        self.written = []
        finished = False
        try:
            result = self.drive(schema, root, view)
            finished = True
        finally:
            if not finished:
                self.forget()
        return result

    def forget(self) -> None:
        for key in self.written:
            del self.memo[key]
        self.active.clear()

    def remember(self, key: MemoKey, cel_type: CelType) -> None:
        if key not in self.memo:
            self.written.append(key)
        self.memo[key] = cel_type

    def drive(self, schema: JsonValue, root: JsonValue, view: SchemaView) -> CelType:
        stack: list[tuple[MemoKey, Mapping]] = []
        result = self.start(schema, root, view, stack)
        while stack:
            key, mapping = stack[-1]
            try:
                child, child_root = (
                    next(mapping) if result is None else mapping.send(result)
                )
            except StopIteration as finished:
                stack.pop()
                self.active.discard(key)
                self.remember(key, finished.value)
                result = finished.value
                continue
            result = self.start(child, child_root, view, stack)
        if result is None:
            raise SchemaError("a schema mapping did not finish")
        return result

    def start(
        self,
        schema: JsonValue,
        root: JsonValue,
        view: SchemaView,
        stack: list[tuple[MemoKey, Mapping]],
    ) -> CelType | None:
        located = self.target(schema, root)
        if located is None:
            return DYN
        found, found_root = located
        if not isinstance(found, dict):
            return DYN
        key = (id(found), id(found_root), view)
        known = self.memo.get(key)
        if known is not None:
            return known
        if key in self.active:
            return DYN
        self.meter.tick()
        self.held.append(found)
        self.active.add(key)
        stack.append((key, self.classify(found, found_root, view, key)))
        return None

    def classify(
        self, schema: JsonObject, root: JsonValue, view: SchemaView, key: MemoKey
    ) -> Mapping:
        check_keywords(schema)
        if is_null_schema(schema):
            return NULL if view is SchemaView.STRUCTURED_OUTPUT else DYN
        declared = schema.get("type")
        values = enumerated(schema)
        kind: str | None
        if isinstance(declared, list):
            names = strings(cast("list[JsonValue]", declared))
            others = {name for name in names if name != NULL_TYPE}
            if len(others) != 1:
                return DYN
            kind = others.pop()
            wrap = NULL_TYPE in names
        elif isinstance(declared, str):
            kind, wrap = declared, False
        elif declared is not None:
            raise SchemaError("a schema type is not a string or a list")
        elif values is not None:
            kinds = {json_type(value) for value in values}
            wrap = NULL_TYPE in kinds
            kinds.discard(NULL_TYPE)
            if len(kinds) != 1:
                return DYN
            kind = kinds.pop()
        else:
            other = self.nullable_pair(schema, root)
            if other is not None:
                return nullable((yield other, root))
            kind, wrap = keyword_kind(schema), False
        if kind is None:
            return DYN
        if kind == "object" and isinstance(schema.get("properties"), dict):
            return (yield from self.record(schema, root, key, wrap))
        mapped = yield from self.typed(schema, root, kind, values)
        return nullable(mapped) if wrap else mapped

    def nullable_pair(self, schema: JsonObject, root: JsonValue) -> JsonValue | None:
        for name in ("anyOf", "oneOf"):
            options = cast("list[JsonValue] | None", entry(schema, name, list))
            if options is None or len(options) != 2:
                continue
            first, second = options
            if self.null_member(first, root) != self.null_member(second, root):
                return second if self.null_member(first, root) else first
        return None

    def null_member(self, option: JsonValue, root: JsonValue) -> bool:
        located = self.target(option, root)
        return (
            located is not None
            and isinstance(located[0], dict)
            and is_null_schema(located[0])
        )

    def typed(
        self,
        schema: JsonObject,
        root: JsonValue,
        kind: str,
        values: list[JsonValue] | None,
    ) -> Mapping:
        if kind == "string":
            return self.string(values)
        scalar = SCALAR_TYPES.get(kind)
        if scalar is not None:
            return scalar
        if kind == "array":
            items = schema.get("items")
            if "prefixItems" in schema or items is None:
                return ListOf(DYN)
            return ListOf((yield items, root))
        if kind == "object":
            value = map_value(schema)
            return MapOf(STRING, DYN if value is None else (yield value, root))
        raise SchemaError("a schema type is not a JSON Schema type name")

    def string(self, values: list[JsonValue] | None) -> CelType:
        if values is None:
            return STRING
        allowed: set[str] = set()
        for value in values:
            self.meter.tick()
            if isinstance(value, str):
                allowed.add(value)
            elif value is not None:
                return STRING
        return Primitive(Kind.STRING, frozenset(allowed))

    def record(
        self, schema: JsonObject, root: JsonValue, key: MemoKey, wrap: bool
    ) -> Mapping:
        properties = cast("dict[str, JsonValue]", schema["properties"])
        required = cast("list[JsonValue]", entry(schema, "required", list) or [])
        names = set(strings(required))
        extra_schema = additional_schema(schema)
        extra: CelType | None = DYN if extra_schema is not False else None
        if isinstance(extra_schema, dict):
            extra = yield extra_schema, root
        fields: dict[str, RecordField] = {}
        record = Record(fields, extra)
        mapped = nullable(record) if wrap else record
        self.remember(key, mapped)
        for name, member in properties.items():
            self.meter.tick()
            fields[name] = RecordField((yield member, root), name in names)
        return mapped


def additional_schema(schema: JsonObject) -> JsonValue:
    additional = schema.get("additionalProperties", True)
    patterns = cast("dict[str, JsonValue] | None", schema.get("patternProperties"))
    if patterns is None:
        return additional
    if additional is False and len(patterns) == 1:
        return next(iter(patterns.values()))
    return True


def map_value(schema: JsonObject) -> JsonValue | None:
    value = additional_schema(schema)
    return value if isinstance(value, dict) else None
