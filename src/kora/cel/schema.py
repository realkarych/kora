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
    Dyn,
    Kind,
    ListOf,
    MapOf,
    Nullable,
    Primitive,
    Record,
    RecordField,
)

JsonObject = dict[str, JsonValue]
Resolver = Callable[[str], JsonValue | None]
Located = tuple[JsonValue, JsonValue]
MemoKey = tuple[int, "SchemaView"]
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


def nullable(cel_type: CelType) -> CelType:
    if isinstance(cel_type, Dyn | Nullable) or cel_type == NULL:
        return cel_type
    return Nullable(cel_type)


def entry(schema: JsonObject, name: str, kind: type) -> object:
    value = schema.get(name)
    if value is not None and not isinstance(value, kind):
        raise SchemaError("a schema keyword has the wrong JSON type")
    return value


def enumerated(schema: JsonObject) -> list[JsonValue] | None:
    if "const" in schema:
        return [schema["const"]]
    values = entry(schema, "enum", list)
    return cast("list[JsonValue] | None", values)


def is_schema(value: JsonValue) -> bool:
    return isinstance(value, dict | bool)


def is_position(name: str, length: int) -> bool:
    return name.isascii() and name.isdigit() and int(name) < length


def is_null(schema: JsonObject) -> bool:
    declared = schema.get("type")
    if declared == NULL_TYPE or declared == [NULL_TYPE]:
        return True
    if "const" in schema:
        return schema["const"] is None
    return schema.get("enum") == [None]


def type_names(declared: list[JsonValue]) -> list[str]:
    names = [name for name in declared if isinstance(name, str)]
    if len(names) != len(declared):
        raise SchemaError("a schema type is not a string")
    return names


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
        self.held: list[JsonValue] = []

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
        for token in fragment[1:].split("/"):
            self.meter.tick()
            name = token.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and name in node:
                node = node[name]
            elif isinstance(node, list) and is_position(name, len(node)):
                node = node[int(name)]
            else:
                raise SchemaError("a schema reference does not resolve")
        return node

    def target(self, schema: JsonValue, root: JsonValue) -> Located | None:
        seen: set[int] = set()
        while isinstance(schema, dict):
            self.meter.tick()
            if id(schema) in seen:
                return None
            seen.add(id(schema))
            reference = entry(schema, "$ref", str)
            parts = entry(schema, "allOf", list)
            if isinstance(reference, str):
                schema, root = self.resolve(root, reference)
            elif isinstance(parts, list) and len(cast("list[JsonValue]", parts)) == 1:
                schema = cast("list[JsonValue]", parts)[0]
            else:
                break
        if not is_schema(schema):
            raise SchemaError("a schema is not an object or a boolean")
        return schema, root

    def mapped(self, schema: JsonValue, root: JsonValue, view: SchemaView) -> CelType:
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
                self.memo[key] = finished.value
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
        key = (id(found), view)
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
        if is_null(schema):
            return NULL if view is SchemaView.STRUCTURED_OUTPUT else DYN
        declared = schema.get("type")
        values = enumerated(schema)
        kind: str | None
        if isinstance(declared, list):
            names = type_names(cast("list[JsonValue]", declared))
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
            located is not None and isinstance(located[0], dict) and is_null(located[0])
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
        names = {name for name in required if isinstance(name, str)}
        if len(names) != len(set(map(str, required))):
            raise SchemaError("a required property name is not a string")
        additional = schema.get("additionalProperties")
        extra: CelType | None = None if additional is False else DYN
        if isinstance(additional, dict):
            extra = yield additional, root
        fields: dict[str, RecordField] = {}
        record = Record(fields, extra)
        mapped = nullable(record) if wrap else record
        self.memo[key] = mapped
        for name, member in properties.items():
            self.meter.tick()
            fields[name] = RecordField((yield member, root), name in names)
        return mapped


def map_value(schema: JsonObject) -> JsonValue | None:
    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        return cast("JsonValue", additional)
    patterns = entry(schema, "patternProperties", dict)
    if isinstance(patterns, dict):
        options = cast("dict[str, JsonValue]", patterns)
        if len(options) == 1:
            return next(iter(options.values()))
    return None
