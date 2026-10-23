import hashlib
import json
from collections.abc import Callable
from typing import cast

from pydantic import BaseModel

from kora.ir.document import GraphDocument
from kora.ir.schema import generate_ir_schema, render_ir_schema


def serialization_schema() -> dict[str, object]:
    return GraphDocument.model_json_schema(by_alias=True, mode="serialization")


def render_schema_utf8() -> bytes:
    return render_ir_schema().encode("utf-8")


SCHEMA_ACTIONS: tuple[Callable[[], object], ...] = (
    generate_ir_schema,
    serialization_schema,
    render_schema_utf8,
)


def schema_models() -> dict[str, type[BaseModel]]:
    models: dict[str, type[BaseModel]] = {}
    pending: list[object] = [GraphDocument.__pydantic_core_schema__]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if id(cast("object", item)) in seen:
                continue
            seen.add(id(cast("object", item)))
            fields = cast("dict[str, object]", item)
            model = fields.get("cls")
            if isinstance(model, type) and issubclass(model, BaseModel):
                models[model.__name__] = model
            pending.extend(fields.values())
        elif isinstance(item, list | tuple):
            pending.extend(cast("list[object] | tuple[object, ...]", item))
    return models


def schema_inventory(schema: dict[str, object], encoded: bytes) -> dict[str, object]:
    definitions = cast("dict[str, object]", schema["$defs"])
    references = 0
    pending: list[object] = [schema]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            fields = cast("dict[str, object]", item)
            references += "$ref" in fields
            pending.extend(fields.values())
        elif isinstance(item, list):
            pending.extend(cast("list[object]", item))
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
        "definitions": len(definitions),
        "references": references,
        "definition_names": sorted(definitions),
    }


def schema_fixture_manifest() -> str:
    validation = generate_ir_schema()
    serialization = serialization_schema()
    rendered = render_schema_utf8()
    return json.dumps(
        {
            "validation": schema_inventory(
                validation,
                (json.dumps(validation, indent=2, ensure_ascii=False) + "\n").encode(),
            ),
            "serialization": schema_inventory(
                serialization,
                (
                    json.dumps(serialization, indent=2, ensure_ascii=False) + "\n"
                ).encode(),
            ),
            "render_utf8": schema_inventory(validation, rendered),
            "class_schema_inventory": sorted(schema_models()),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def schema_workloads() -> dict[str, tuple[Callable[[], object], int, bool, int]]:
    return {
        "schema_validation": (generate_ir_schema, 1, True, 30),
        "schema_serialization": (serialization_schema, 1, True, 30),
        "schema_render": (render_ir_schema, 1, True, 30),
        "schema_render_utf8": (render_schema_utf8, 1, True, 30),
    }


def schema_cycle(index: int) -> None:
    SCHEMA_ACTIONS[index % 3]()
