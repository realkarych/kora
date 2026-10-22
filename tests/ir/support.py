import json
from pathlib import Path
from typing import Protocol, cast

from pydantic import ValidationError
from ruamel.yaml import YAML

from kora.ir.errors import safe_field_errors

ROOT: Path = Path(__file__).parents[2]
SCHEMA_PATH: Path = ROOT / "knowledge" / "specs" / "ir.schema.json"
EXAMPLES: Path = ROOT / "knowledge" / "specs" / "examples"
POLICY_FIELDS: frozenset[str] = frozenset({"allow", "deny", "extensions"})
SAFE_MESSAGE: str = "the tool policy is invalid"


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


def load_yaml(path: Path) -> object:
    loader = cast("YamlLoader", YAML(typ="safe"))
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
