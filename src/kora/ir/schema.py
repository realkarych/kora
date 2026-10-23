import json

from kora.ir.contracts import SCHEMA_DIALECT
from kora.ir.document import GraphDocument


def generate_ir_schema() -> dict[str, object]:
    return {
        "$schema": SCHEMA_DIALECT,
        "$id": "https://kora.dev/schemas/ir/1.0.json",
        **GraphDocument.model_json_schema(by_alias=True, mode="validation"),
    }


def render_ir_schema() -> str:
    return json.dumps(generate_ir_schema(), indent=2, ensure_ascii=False) + "\n"
