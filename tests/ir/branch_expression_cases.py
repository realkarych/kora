from kora.ir.document import GraphDocument
from kora.ir.json import JsonObject
from tests.ir.approval_input_cases import BUILTINS, producer
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for

SCHEMA: JsonObject = {
    "properties": {
        "ok": {"type": "boolean"},
        "label": {"type": "string"},
        "mode": {"enum": ["run", "stop"]},
    },
    "required": ["ok", "label", "mode"],
    "additionalProperties": False,
}


def branch(
    when: str = "true",
    reason: str | None = None,
    else_reason: str | None = None,
    **fields: object,
) -> dict[str, object]:
    return node(
        "branch",
        config={
            "cases": [
                {
                    "when": when,
                    "to": ["target"],
                    **({} if reason is None else {"reason": reason}),
                }
            ],
            **({} if else_reason is None else {"else_reason": else_reason}),
        },
        **fields,
    )


def branch_document(
    when: str = "true",
    reason: str | None = None,
    else_reason: str | None = None,
    *,
    nested: bool = False,
    optional: bool = False,
) -> GraphDocument:
    nodes = {
        "producer": producer(schema=SCHEMA),
        "branch": branch(
            when,
            reason,
            else_reason,
            inputs={
                "data": {
                    "type": "StructuredOutput",
                    "from": "producer",
                    "output": "value",
                    "optional": optional,
                }
            },
            source={"file": "branch.py", "line": 7},
        ),
        "target": node(depends_on=["branch"]),
    }
    return document_for(
        params=SCHEMA,
        contracts=BUILTINS,
        nodes={"timer": node()} if nested else nodes,
        bodies={"unused": {"nodes": nodes}} if nested else None,
    )
