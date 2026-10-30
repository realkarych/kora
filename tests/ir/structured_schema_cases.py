import json
from collections.abc import Iterator
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Never, cast

from kora.ir import (
    PreparedStructuredSchemaInput,
    StructuredSchemaPreparationInput,
    StructuredSchemaPreparationRefusal,
    prepare_structured_schema_input,
)
from kora.ir.structured_schema_input import SchemaNode

ARTIFACTS: Path = Path(__file__).resolve().parents[2] / "knowledge/specs/artifacts"


def parameters() -> dict[str, int]:
    return {
        "max_input_bytes": 1_048_576,
        "max_input_nodes": 16_384,
        "max_input_depth": 32,
        "max_schema_documents": 64,
        "max_schema_positions": 2_048,
        "max_reference_edges": 4_096,
        "max_numeric_bits": 14_000,
        "max_preparation_work": 16_777_216,
        "max_auxiliary_bytes": 4_194_304,
        "max_auxiliary_entries": 32_768,
    }


def source(name: str, kind: str = "type_schema") -> dict[str, object]:
    return {
        "kind": kind,
        "path": ["type_contracts", name, "schema"],
        "location": None,
    }


def builtin() -> dict[str, object]:
    descriptor = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "StructuredOutput.revision-1.json").read_bytes()),
    )
    return {
        "name": "StructuredOutput",
        "builtin_revision": "1",
        "parent": None,
        "behavior": descriptor["behavior"],
        "descriptor": descriptor,
        "source": source("StructuredOutput"),
        "schema": json.loads((ARTIFACTS / "StructuredOutput.schema.json").read_bytes()),
    }


def contract(
    schema: object,
    name: str = "example/Declared",
    parent: str | None = "StructuredOutput",
) -> dict[str, object]:
    return {
        "name": name,
        "builtin_revision": None,
        "parent": parent,
        "behavior": {
            "validation": "example.validation.v1",
            "digest_fields": "example.digest.v1",
            "summary": "example.summary.v1",
        },
        "descriptor": None,
        "source": source(name),
        "schema": schema,
    }


def preparation_input(
    schema: object,
    *,
    records: list[dict[str, object]] | None = None,
    declared_type: str = "example/Declared",
    inline: object = None,
) -> StructuredSchemaPreparationInput:
    return StructuredSchemaPreparationInput(
        declared_type=declared_type,
        type_contracts=[builtin(), contract(schema)] if records is None else records,
        inline_schema=inline,
        execution_semantics="kora.execution.v1",
        checker_binding="kora.structured-output-view.v1",
        preparation_parameters=parameters(),
    )


def prepared(value: StructuredSchemaPreparationInput) -> PreparedStructuredSchemaInput:
    result = prepare_structured_schema_input(value)
    assert isinstance(result, PreparedStructuredSchemaInput)
    return result


def refusal(value: object) -> str:
    result = prepare_structured_schema_input(value)
    assert isinstance(result, StructuredSchemaPreparationRefusal)
    return result.reason


def native_counts(value: object) -> tuple[int, int]:
    nodes = 0
    depth = 0
    pending = [(value, 0)]
    while pending:
        item, parent_depth = pending.pop()
        nodes += 1
        if type(item) is dict:
            members = cast("dict[str, object]", item)
            current_depth = parent_depth + 1
            depth = max(depth, current_depth)
            nodes += len(members)
            pending.extend((child, current_depth) for child in members.values())
        elif type(item) is list:
            current_depth = parent_depth + 1
            depth = max(depth, current_depth)
            pending.extend(
                (child, current_depth) for child in cast("list[object]", item)
            )
    return nodes, depth


def reachable_container_ids(value: object) -> set[int]:
    seen: set[int] = set()
    containers: set[int] = set()
    pending = [value]
    while pending:
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(item, dict):
            containers.add(identity)
            pending.extend(cast("dict[object, object]", item).values())
        elif isinstance(item, list | tuple):
            containers.add(identity)
            pending.extend(cast("list[object] | tuple[object, ...]", item))
        elif is_dataclass(item) and not isinstance(item, type):
            pending.extend(getattr(item, field.name) for field in fields(item))
    return containers


def normalized_input(value: PreparedStructuredSchemaInput) -> object:
    arena = arena_nodes(value)

    def decode(index: int) -> object:
        node = arena[index]
        if node.kind == "scalar":
            return node.value
        if node.kind == "array":
            return [decode(child) for child in node.children]
        return {
            cast("str", arena[key].value): decode(child) for key, child in node.members
        }

    return decode(cast("int", stored_field(value, "_root")))


def arena_nodes(value: PreparedStructuredSchemaInput) -> tuple[SchemaNode, ...]:
    return cast("tuple[SchemaNode, ...]", stored_field(value, "_arena"))


def stored_field(value: PreparedStructuredSchemaInput, name: str) -> object:
    return next(
        getattr(value, field.name) for field in fields(value) if field.name == name
    )


class HostileString(str):
    def __str__(self) -> str:
        raise AssertionError("authored conversion invoked")


class HostileInteger(int):
    def __int__(self) -> int:
        raise AssertionError("authored conversion invoked")


class HostileDictionary(dict[str, object]):
    def items(self) -> Never:
        raise AssertionError("authored mapping invoked")

    def __len__(self) -> int:
        raise AssertionError("authored length invoked")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("authored iteration invoked")


class HostileArray(list[object]):
    def __len__(self) -> int:
        raise AssertionError("authored length invoked")

    def __iter__(self) -> Iterator[object]:
        raise AssertionError("authored iteration invoked")


class HostileHeaderString(str):
    def __eq__(self, other: object) -> bool:
        raise AssertionError("authored comparison invoked")

    def __ne__(self, other: object) -> bool:
        raise AssertionError("authored comparison invoked")

    def __hash__(self) -> int:
        return str.__hash__(self)

    def __len__(self) -> int:
        raise AssertionError("authored length invoked")


class HostileHeader:
    def __eq__(self, other: object) -> bool:
        raise AssertionError("authored comparison invoked")

    def __ne__(self, other: object) -> bool:
        raise AssertionError("authored comparison invoked")

    def __len__(self) -> int:
        raise AssertionError("authored length invoked")

    def __iter__(self) -> Iterator[object]:
        raise AssertionError("authored iteration invoked")


def closed_documents(schemas: list[object]) -> StructuredSchemaPreparationInput:
    names = [f"example/Target{index:02d}" for index in range(len(schemas))]
    declared = {
        "allOf": [
            {"$ref": "urn:kora:artifact:" + name.replace("/", ":")} for name in names
        ]
    }
    records = [builtin(), contract(declared)]
    records.extend(
        contract(schema, name) for name, schema in zip(names, schemas, strict=True)
    )
    return preparation_input({}, records=records)
