from kora.ir import GraphDocument
from kora.ir.json import JsonObject
from tests.ir.type_declaration_cases import custom, model


def document_for(schema: JsonObject) -> GraphDocument:
    contract = custom()
    contract["schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **schema,
    }
    return model({"example/Value": contract})


def nested_schema(depth: int, leaf: JsonObject | None = None) -> JsonObject:
    result: JsonObject = {} if leaf is None else leaf
    for _ in range(depth):
        result = {"items": result}
    return result


def wide_schema(count: int, invalid: bool = False) -> JsonObject:
    return {
        "properties": {
            f"field_{index}": {
                "type": "invalid" if invalid else "string",
                "$ref": "#/$defs/shared",
            }
            for index in range(count)
        },
        "$defs": {"shared": True},
    }
