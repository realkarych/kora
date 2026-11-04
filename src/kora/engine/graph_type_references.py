from collections.abc import Iterator
from dataclasses import dataclass

from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    TextBudget,
    render_object,
    scalar_text,
)
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_locations import (
    LISTED_SCHEMAS,
    NAMED_SCHEMAS,
    SINGLE_SCHEMAS,
    Position,
    Schema,
    decode_fragment,
    next_position,
)
from kora.ir.schema_validation import REFERENCE_KEYWORDS

NAMED: frozenset[str] = NAMED_SCHEMAS | {"dependencies"}


@dataclass(frozen=True, slots=True)
class SchemaPath:
    parent: "SchemaPath | None"
    segment: str | int
    depth: int


def child_path(parent: SchemaPath | None, segment: str | int) -> SchemaPath:
    return SchemaPath(parent, segment, 1 if parent is None else parent.depth + 1)


def array_text(value: object, budget: TextBudget) -> str:
    text = render_object({"v": value}, TextBudget(budget.remaining + 6))
    budget.reserve(len(text) - 6)
    return text[5:-1]


def path_text(path: SchemaPath, budget: TextBudget) -> str:
    if 2 * path.depth + 1 > budget.remaining:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    segments: list[JsonValue] = []
    current: SchemaPath | None = path
    while current is not None:
        segments.append(current.segment)
        current = current.parent
    segments.reverse()
    return array_text(segments, budget)


def children(
    schema: JsonObject, path: SchemaPath | None
) -> Iterator[tuple[Schema, SchemaPath]]:
    for keyword, value in schema.items():
        if keyword in SINGLE_SCHEMAS and isinstance(value, dict | bool):
            yield value, child_path(path, keyword)
        elif keyword in NAMED and isinstance(value, dict):
            field = child_path(path, keyword)
            for name, member in value.items():
                if isinstance(member, dict | bool):
                    yield member, child_path(field, name)
        elif keyword in LISTED_SCHEMAS and isinstance(value, list):
            field = child_path(path, keyword)
            for index, member in enumerate(value):
                if isinstance(member, dict | bool):
                    yield member, child_path(field, index)


def schema_sites(root: JsonObject) -> Iterator[tuple[JsonObject, SchemaPath | None]]:
    pending: list[Iterator[tuple[Schema, SchemaPath | None]]] = [iter(((root, None),))]
    while pending:
        entry = next(pending[-1], None)
        if entry is None:
            pending.pop()
            continue
        schema, path = entry
        if isinstance(schema, dict):
            yield schema, path
            pending.append(children(schema, path))


def target_path(root: JsonObject, fragment: str, budget: TextBudget) -> str:
    pointer = decode_fragment(fragment)
    if pointer is None or (pointer and not pointer.startswith("/")):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    budget.reserve(2)
    value: JsonValue = root
    position = Position.SCHEMA
    rendered: list[str] = []
    for token in () if not pointer else pointer[1:].split("/"):
        if any(
            character == "~"
            and (index + 1 == len(token) or token[index + 1] not in "01")
            for index, character in enumerate(token)
        ):
            raise GraphValueRejected(GraphValueFailure.INVALID)
        key = token.replace("~1", "/").replace("~0", "~")
        segment: str | int
        if isinstance(value, dict) and key in value:
            segment = key
            value = value[key]
        elif (
            isinstance(value, list)
            and key.isascii()
            and key.isdigit()
            and len(key) <= len(str(len(value)))
            and (key == "0" or not key.startswith("0"))
            and int(key) < len(value)
        ):
            segment = int(key)
            value = value[segment]
        else:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        position = next_position(position, key, value)
        if position is Position.SCHEMA and isinstance(value, dict) and "$id" in value:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        budget.reserve(int(bool(rendered)))
        rendered.append(scalar_text(segment, budget))
    if position is not Position.SCHEMA or not isinstance(value, dict | bool):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return "[" + ",".join(rendered) + "]"


def reference_text(
    name: str, roots: dict[str, JsonObject], uris: dict[str, str], budget: TextBudget
) -> str:
    return root_reference_text(roots[name], name, roots, uris, budget)


def root_reference_text(
    root: JsonObject,
    name: str | None,
    roots: dict[str, JsonObject],
    uris: dict[str, str],
    budget: TextBudget,
) -> str:
    budget.reserve(2)
    records: list[tuple[str, str]] = []
    for schema, location in schema_sites(root):
        for keyword, value in schema.items():
            if keyword not in REFERENCE_KEYWORDS:
                continue
            if not isinstance(value, str):
                raise GraphValueRejected(GraphValueFailure.INVALID)
            budget.reserve(
                len('{"path":,"target_type":,"target_path":}') + int(bool(records))
            )
            source = path_text(child_path(location, keyword), budget)
            base, _, fragment = value.partition("#")
            target = uris.get(base) if base else name
            if base and target is None:
                raise GraphValueRejected(GraphValueFailure.INVALID)
            target_name = scalar_text(target, budget)
            destination = target_path(
                root if not base else roots[str(target)], fragment, budget
            )
            text = (
                '{"path":'
                + source
                + ',"target_type":'
                + target_name
                + ',"target_path":'
                + destination
                + "}"
            )
            records.append((source, text))
    records.sort(key=lambda record: record[0])
    return "[" + ",".join(text for _, text in records) + "]"
