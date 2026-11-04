from kora.db.graph_value_json import ObjectReader, TextBudget, render_object
from kora.engine.graph_types import build_graph_type_contracts
from kora.ir import GraphDocument, render_ir_json
from kora.ir.contracts import SCHEMA_DIALECT
from kora.ir.json import JsonObject, JsonValue
from tests.ir.document_cases import document
from tests.ir.structured_schema_cases import parameters


def declaration(
    schema: JsonObject, parent: str | None = "StructuredOutput"
) -> JsonObject:
    return {
        "schema": {"$schema": SCHEMA_DIALECT, **schema},
        "parent": parent,
        "behavior": {
            "validation": "example.validation.v1",
            "digest_fields": "example.digest.v1",
            "summary": "example.summary.v1",
        },
    }


def saved_types(contracts: JsonObject) -> tuple[str, list[JsonValue]]:
    raw = document()
    raw["type_contracts"] = {"StructuredOutput": {"builtin_revision": "1"}, **contracts}
    ir_text = render_ir_json(GraphDocument.model_validate(raw))
    text = build_graph_type_contracts(ir_text, max_encoded_bytes=10_000_000)
    records = ObjectReader('{"v":' + text + "}").object()["v"]
    assert isinstance(records, list)
    return ir_text, records


def schema_candidate(
    schema: JsonObject | bool | None = None, declared: str = "StructuredOutput"
) -> JsonObject:
    return {
        "declared_type": declared,
        "inline_schema": None
        if schema is None
        else {
            "source": {
                "kind": "inline_schema",
                "path": ["graph", "nodes", "example", "outputs", "value", "schema"],
                "location": {
                    "file": "private.py",
                    "line": 7,
                    "x-source": {"value": "\0"},
                },
            },
            "schema": schema,
        },
        "execution_semantics": "kora.execution.v1",
        "checker_binding": "kora.structured-output-view.v1",
        "schema_validation": {
            "policy": "kora.structured-output-schema-policy.v1",
            "parameters": {
                **parameters(),
                "max_numeric_intermediate_bits": 16_384,
                "max_evaluation_work": 16_777_216,
                "max_evaluation_depth": 64,
            },
        },
    }


def input_size(ir_text: str, types: list[JsonValue], candidate: JsonObject) -> int:
    return (
        len(ir_text)
        + len(render_object({"v": types}, TextBudget(10_000_000)))
        - 6
        + len(render_object(candidate, TextBudget(10_000_000)))
    )
