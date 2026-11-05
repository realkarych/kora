from kora.ir.document import GraphDocument
from tests.ir.approval_input_cases import BUILTINS, producer
from tests.ir.branch_expression_cases import SCHEMA
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for


def retrying(
    text: str | None = "true",
    declaration: dict[str, object] | None = None,
    *,
    attempts: int = 1,
    **fields: object,
) -> dict[str, object]:
    return {
        **(node("command") if declaration is None else declaration),
        **fields,
        "retry": {
            "max_attempts": attempts,
            **({} if text is None else {"predicate": text}),
        },
    }


def retry_document(text: str = "true", *, nested: bool = False) -> GraphDocument:
    nodes = {
        "producer": producer(schema=SCHEMA),
        "check": retrying(
            text,
            producer(schema=SCHEMA),
            inputs={
                "data": {
                    "type": "StructuredOutput",
                    "from": "producer",
                    "output": "value",
                }
            },
            source={"file": "retry.py", "line": 7},
        ),
    }
    return document_for(
        params=SCHEMA,
        contracts=BUILTINS,
        nodes={"timer": node()} if nested else nodes,
        bodies={"unused": {"nodes": nodes}} if nested else None,
    )
