from collections.abc import Callable
from typing import cast

import pytest
from pydantic import TypeAdapter

from kora.ir import (
    CommandConfig,
    CommandResource,
    ErrorClass,
    McpServer,
    NetworkPolicy,
    NodeLimits,
    ToolPolicy,
    Workspace,
)
from tests.ir.support import schema_definition

IGNORED_KEYS: frozenset[str] = frozenset({"title", "description", "default"})
GENERATED: dict[str, Callable[[], dict[str, object]]] = {
    "errorClass": lambda: TypeAdapter(ErrorClass).json_schema(),
    "errorClass_serialisation": lambda: TypeAdapter(ErrorClass).json_schema(
        mode="serialization"
    ),
    "toolPolicy": ToolPolicy.model_json_schema,
    "toolPolicy_serialisation": lambda: ToolPolicy.model_json_schema(
        mode="serialization"
    ),
}
CONFIG_MODELS = {
    "limits": NodeLimits,
    "workspace": Workspace,
    "mcpServer": McpServer,
    "commandConfig": CommandConfig,
    "commandResource": CommandResource,
    "networkPolicy_agentConfig": NetworkPolicy,
    "networkPolicy_commandConfig": NetworkPolicy,
    "networkPolicy_mcpServer": NetworkPolicy,
}
for definition, model in CONFIG_MODELS.items():
    GENERATED[definition] = model.model_json_schema
    GENERATED[definition + "_serialisation"] = lambda model=model: (
        model.model_json_schema(mode="serialization")
    )
REFERENCES: frozenset[str] = frozenset(
    {
        "duration",
        "digest",
        "envName",
        "workspace",
        "Workspace",
        "NetworkPolicy",
        "CommandResource",
    }
)


def property_schema(record: str, name: str) -> object:
    definition = cast("dict[str, object]", schema_definition(record))
    return cast("dict[str, object]", definition["properties"])[name]


def committed_schema(definition: str) -> object:
    if definition == "commandResource":
        resources = cast(
            "dict[str, object]", property_schema("commandConfig", "resources")
        )
        return resources["items"]
    if definition.startswith("networkPolicy_"):
        return property_schema(
            definition.removeprefix("networkPolicy_"), "network_policy"
        )
    return schema_definition(definition)


def inline_known_references(value: object, definitions: dict[str, object]) -> object:
    if isinstance(value, dict):
        items = cast("dict[str, object]", value)
        if "$ref" in items:
            reference = items["$ref"]
            assert isinstance(reference, str)
            assert reference.startswith("#/$defs/")
            assert set(items) <= {"$ref", "title", "description"}
            name = reference.removeprefix("#/$defs/")
            assert name in REFERENCES
            target = (
                definitions[name] if name in definitions else schema_definition(name)
            )
            assert target is not None
            return inline_known_references(target, definitions)
        return {
            key: inline_known_references(item, definitions)
            for key, item in items.items()
        }
    if isinstance(value, list):
        return [
            inline_known_references(item, definitions)
            for item in cast("list[object]", value)
        ]
    return value


def normalised(value: object, names: bool = False) -> object:
    if isinstance(value, dict):
        items = cast("dict[str, object]", value)
        return {
            key: normalised(item, key == "properties" and not names)
            for key, item in items.items()
            if names or key not in IGNORED_KEYS
        }
    if isinstance(value, list):
        return [normalised(item) for item in cast("list[object]", value)]
    return value


@pytest.mark.parametrize("definition", sorted(GENERATED))
def test_ir_models_match_schema_definitions(definition: str) -> None:
    committed = committed_schema(definition.removesuffix("_serialisation"))

    generated = GENERATED[definition]()
    definitions = cast("dict[str, object]", generated.pop("$defs", {}))

    assert set(definitions) <= REFERENCES
    assert normalised(inline_known_references(generated, definitions)) == normalised(
        inline_known_references(committed, {})
    )
