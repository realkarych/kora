from collections.abc import Callable
from typing import cast

import pytest
from pydantic import TypeAdapter

from kora.ir import ErrorClass, ToolPolicy
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
    committed = schema_definition(definition.removesuffix("_serialisation"))

    generated = GENERATED[definition]()

    assert normalised(generated) == normalised(committed)
