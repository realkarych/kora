from kora.ir import GraphDocument
from kora.ir.json import JsonObject, JsonValue
from tests.ir.expression_syntax_cases import set_field
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.output_schema_cases import human
from tests.ir.type_declaration_cases import custom


def decision_form(member: JsonValue, required: bool = True) -> JsonObject:
    return {
        "type": "object",
        "properties": {"decision": member},
        "required": ["decision"] if required else [],
    }


def gate(
    form: JsonObject | None = None, predicate: str | None = None, **fields: object
) -> dict[str, object]:
    value = human(form, None, **fields)
    if predicate is not None:
        set_field(value, ("config", "approve_when"), predicate)
    return value


def saved_form(schema: JsonObject) -> dict[str, object]:
    return {
        **custom(),
        "schema": {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema},
    }


def shared_form(count: int, depth: int, invalid: bool = False) -> GraphDocument:
    schema: JsonObject = decision_form(
        {"enum": ["reject"] if invalid else ["approve", "reject"]}
    )
    for _ in range(depth):
        schema = {"allOf": [schema]}
    return document_for(
        contracts={"example/Form": saved_form(schema)},
        nodes={
            f"n{index}": gate({"$ref": "urn:kora:artifact:example:Form"})
            for index in range(count)
        },
    )


def stored_bodies(count: int) -> GraphDocument:
    return document_for(
        nodes={
            f"n{index}": node("subgraph", config={"body": "shared"})
            for index in range(count)
        },
        bodies={
            "shared": {
                "nodes": {
                    "gate": gate(),
                    "nested": node("subgraph", config={"body": "unused"}),
                }
            },
            "unused": {"nodes": {"gate": gate({})}},
        },
    )
