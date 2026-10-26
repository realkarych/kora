from collections.abc import Callable
from typing import cast

import pytest
from pydantic import TypeAdapter

from kora.ir import (
    AgentConfig,
    BranchCase,
    BranchConfig,
    CommandConfig,
    CommandResource,
    ErrorClass,
    EventSelector,
    FanOutConfig,
    HumanInputBinds,
    HumanInputConfig,
    McpServer,
    NetworkPolicy,
    NodeLimits,
    Retry,
    SensorPoll,
    SubagentConfig,
    SubgraphNodeConfig,
    TimerConfig,
    ToolPolicy,
    WaitEventConfig,
    Workspace,
)
from tests.ir.support import schema_definition

IGNORED_KEYS: frozenset[str] = frozenset({"title", "description", "default"})
GENERATED: dict[str, Callable[[], dict[str, object]]] = {
    "ErrorClass": lambda: TypeAdapter(ErrorClass).json_schema(),
    "ErrorClass_serialisation": lambda: TypeAdapter(ErrorClass).json_schema(
        mode="serialization"
    ),
    "ToolPolicy": ToolPolicy.model_json_schema,
    "ToolPolicy_serialisation": lambda: ToolPolicy.model_json_schema(
        mode="serialization"
    ),
}
CONFIG_MODELS = {
    "AgentConfig": AgentConfig,
    "SubagentConfig": SubagentConfig,
    "HumanInputBinds": HumanInputBinds,
    "HumanInputConfig": HumanInputConfig,
    "EventSelector": EventSelector,
    "SensorPoll": SensorPoll,
    "WaitEventConfig": WaitEventConfig,
    "TimerConfig": TimerConfig,
    "BranchCase": BranchCase,
    "BranchConfig": BranchConfig,
    "FanOutConfig": FanOutConfig,
    "SubgraphNodeConfig": SubgraphNodeConfig,
    "Retry": Retry,
    "NodeLimits": NodeLimits,
    "Workspace": Workspace,
    "McpServer": McpServer,
    "CommandConfig": CommandConfig,
    "CommandResource": CommandResource,
    "NetworkPolicy": NetworkPolicy,
}
for definition, model in CONFIG_MODELS.items():
    GENERATED[definition] = model.model_json_schema
    GENERATED[definition + "_serialisation"] = lambda model=model: (
        model.model_json_schema(mode="serialization")
    )


def property_schema(record: str, name: str) -> object:
    definition = cast("dict[str, object]", schema_definition(record))
    return cast("dict[str, object]", definition["properties"])[name]


def inline_known_references(value: object, definitions: dict[str, object]) -> object:
    if isinstance(value, dict):
        items = cast("dict[str, object]", value)
        if "$ref" in items:
            reference = items["$ref"]
            assert isinstance(reference, str)
            assert reference.startswith("#/$defs/")
            assert set(items) <= {"$ref", "title", "description"}
            name = reference.removeprefix("#/$defs/")
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
    generated = GENERATED[definition]()
    committed = schema_definition(cast("str", generated["title"]))
    definitions = cast("dict[str, object]", generated.pop("$defs", {}))

    assert normalised(inline_known_references(generated, definitions)) == normalised(
        inline_known_references(committed, {})
    )


def test_human_input_form_default_is_an_annotation_without_inserted_values() -> None:
    from kora.ir import export_ir

    expected = {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["approve", "reject"]},
            "comments": {"type": "string"},
        },
        "required": ["decision"],
        "additionalProperties": False,
    }
    committed = cast("dict[str, object]", property_schema("HumanInputConfig", "form"))
    generated = HumanInputConfig.model_json_schema()
    properties = cast("dict[str, dict[str, object]]", generated["properties"])
    document = {"channel": "cli", "timeout": "0s", "on_expiry": "fail"}

    parsed = HumanInputConfig.model_validate(document)

    assert committed["default"] == properties["form"]["default"] == expected
    assert export_ir(parsed) == document


def test_timer_timestamp_pattern_and_format_are_present_in_both_schema_modes() -> None:
    expected = (
        r"^(?!0000)[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])[Tt]"
        r"([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,6})?"
        r"([Zz]|[+-]([01][0-9]|2[0-3]):[0-5][0-9])(?![\s\S])"
    )
    committed = cast("dict[str, object]", property_schema("TimerConfig", "at"))

    schemas = [
        TimerConfig.model_json_schema(),
        TimerConfig.model_json_schema(mode="serialization"),
    ]

    assert committed["format"] == "date-time"
    assert committed["pattern"] == expected
    for schema in schemas:
        timestamp = cast("dict[str, dict[str, object]]", schema["properties"])["at"]
        assert timestamp["format"] == "date-time"
        assert timestamp["pattern"] == expected
