import json
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.constructor import SafeConstructor
from ruamel.yaml.nodes import ScalarNode

from kora.ir.errors import PLACEHOLDER_LOCATION, SafeFieldError, safe_field_errors
from kora.ir.json import JsonObject, JsonValue

ROOT: Path = Path(__file__).parents[2]
SCHEMA_PATH: Path = ROOT / "knowledge" / "specs" / "ir.schema.json"
EXAMPLES: Path = ROOT / "knowledge" / "specs" / "examples"
POLICY_FIELDS: frozenset[str] = frozenset({"allow", "deny", "extensions"})
SAFE_MESSAGE: str = "the tool policy is invalid"
CONFIG_ERROR_MESSAGE: str = "the execution configuration is invalid"


def configuration_errors(error: ValidationError) -> tuple[SafeFieldError, ...]:
    errors = safe_field_errors(error, frozenset(), CONFIG_ERROR_MESSAGE)
    return tuple(
        SafeFieldError(
            tuple(PLACEHOLDER_LOCATION for _ in item.loc), item.type, item.msg
        )
        for item in errors
    )


def schema_definition(name: str) -> object:
    document: object = json.loads(SCHEMA_PATH.read_text())
    if not isinstance(document, dict):
        raise TypeError("the IR schema is an object")
    definitions: object = cast("dict[str, object]", document).get("$defs")
    if not isinstance(definitions, dict):
        raise TypeError("the IR schema has definitions")
    return cast("dict[str, object]", definitions).get(name)


class YamlLoader(Protocol):
    def load(self, stream: Path) -> object: ...


class IrYamlConstructor(SafeConstructor):
    pass


def timestamp_string(constructor: IrYamlConstructor, node: ScalarNode) -> str:
    return cast("TimestampScalar", node).value


class TimestampScalar(Protocol):
    value: str


class TimestampConstructors(Protocol):
    def add_constructor(
        self, tag: str, constructor: Callable[[IrYamlConstructor, ScalarNode], str]
    ) -> None: ...


cast("TimestampConstructors", IrYamlConstructor).add_constructor(
    "tag:yaml.org,2002:timestamp", timestamp_string
)


def load_yaml(path: Path) -> object:
    yaml = YAML(typ="safe")
    yaml.Constructor = IrYamlConstructor
    loader = cast("YamlLoader", yaml)
    return loader.load(path)


def nested(depth: int, leaf: str) -> dict[str, object]:
    document: dict[str, object] = {"leaf": leaf}
    for _ in range(depth - 1):
        document = {"inner": document}
    return document


def error_forms(error: ValidationError) -> str:
    safe = safe_field_errors(error, POLICY_FIELDS, SAFE_MESSAGE)
    return " ".join(
        (
            str(error),
            repr(error),
            error.json(include_input=False, include_context=False),
            json.dumps(error.errors(include_input=False, include_context=False)),
            repr(safe),
        )
    )


def agent_document(data: object) -> dict[str, object]:
    return {
        "harness": "claude",
        "prompt": "",
        "workspace": {"repo": ""},
        "output_schema": data,
    }


def deep_data(depth: int) -> JsonObject:
    value: JsonValue = 0
    for index in reversed(range(depth)):
        value = {"x": value} if index % 2 == 0 else [value]
    return cast("JsonObject", value)
