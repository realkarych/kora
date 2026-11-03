from dataclasses import dataclass
from typing import cast

from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.engine.graph_types import invalid_schema
from kora.ir.approval_form import default_approval_form
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_keywords import KeywordValidation
from kora.ir.schema_locations import Location, SavedReferences
from kora.ir.schema_validation import validate_schema


@dataclass(frozen=True, slots=True)
class GraphOutputSource:
    declared_type: str
    inline: JsonObject | None
    origin: str | None
    source: JsonObject | None


def object_at(root: JsonObject, path: list[str]) -> JsonObject:
    value: JsonValue = root
    for segment in path:
        if not isinstance(value, dict) or segment not in value:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        value = value[segment]
    if not isinstance(value, dict):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return value


def output_path(value: JsonValue) -> list[str]:
    if not isinstance(value, list) or not all(type(item) is str for item in value):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    path = cast(list[str], value)
    if not (
        len(path) == 5
        and path[0:2] == ["graph", "nodes"]
        and path[3] == "outputs"
        or len(path) == 7
        and path[0:2] == ["graph", "subgraphs"]
        and path[3] == "nodes"
        and path[5] == "outputs"
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return path


def checked_schema(schema: JsonValue, roots: dict[str, JsonObject]) -> JsonObject:
    if not isinstance(schema, dict):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    validate_schema(
        schema,
        Location(()),
        SavedReferences(roots),
        KeywordValidation(invalid_schema),
        invalid_schema,
    )
    return schema


def graph_output_source(
    raw: JsonObject, selected: list[str], roots: dict[str, JsonObject]
) -> GraphOutputSource:
    node_path = selected[:-2]
    node = object_at(raw, node_path)
    declaration = object_at(raw, selected)
    config = object_at(node, ["config"])
    kind = node["kind"]
    declared = declaration["type"]
    if not isinstance(declared, str):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    schema: JsonObject | None = None
    origin: str | None = None
    source_path: list[str] | None = None
    if kind == "agent":
        if "schema" in declaration:
            schema = checked_schema(declaration["schema"], roots)
            source_path = [*selected, "schema"]
        if "output_schema" in config:
            schema = checked_schema(config["output_schema"], roots)
            source_path = [*node_path, "config", "output_schema"]
    elif kind == "human_input" and selected[-1] == "response":
        schema = checked_schema(
            config["form"] if "form" in config else default_approval_form(), roots
        )
        source_path = [*node_path, "config", "form"]
        origin = "authored" if "form" in config else "approval_form_default"
    elif kind == "wait_event" and selected[-1] == "event":
        if "schema" in declaration:
            schema = checked_schema(declaration["schema"], roots)
            source_path = [*selected, "schema"]
    elif kind == "command":
        outputs = object_at(node, ["outputs"])
        if "output_file" not in config or len(outputs) != 2 or selected[-1] == "result":
            raise GraphValueRejected(GraphValueFailure.INVALID)
        if "schema" in declaration:
            schema = checked_schema(declaration["schema"], roots)
            source_path = [*selected, "schema"]
    else:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    source: JsonObject | None = None
    if source_path is not None:
        source = {
            "kind": "inline_schema",
            "path": cast(list[JsonValue], source_path),
            "location": node.get("source"),
        }
        if origin is None:
            origin = "authored"
    return GraphOutputSource(declared, schema, origin, source)
