from kora.ir import GraphDocument
from kora.ir.json import JsonObject, JsonValue
from tests.ir.inline_schema_cases import document_for
from tests.ir.type_declaration_cases import custom


def parameter_schema(
    member: JsonValue, required: bool = True, name: str = "target"
) -> JsonObject:
    return {
        "properties": {name: member},
        "required": [name] if required else [],
    }


def target_document(
    params: JsonObject | None,
    parameter: str = "target",
    nodes: dict[str, dict[str, object]] | None = None,
    bodies: dict[str, dict[str, object]] | None = None,
    contracts: dict[str, dict[str, object]] | None = None,
    **fields: object,
) -> GraphDocument:
    return document_for(
        params=params,
        nodes=nodes,
        bodies=bodies,
        contracts=contracts,
        target={"key": {"param": parameter}},
        **fields,
    )


def saved_parameters(schema: JsonObject) -> dict[str, object]:
    return {
        **custom(),
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            **schema,
        },
    }
