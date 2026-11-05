from kora.ir.document import GraphDocument
from tests.ir.approval_input_cases import BUILTINS, producer
from tests.ir.branch_expression_cases import SCHEMA
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for


def wait(
    text: str | None = "true", *, poll: bool = False, **fields: object
) -> dict[str, object]:
    selector = (
        {"poll": {"sensor": "http", "until": text}}
        if poll
        else {
            "event": {
                "type": "webhook",
                "name": "event",
                **({} if text is None else {"match": text}),
            }
        }
    )
    return node(
        "wait_event",
        config={**selector, "timeout": "1h", "on_expiry": "fail"},
        **fields,
    )


def wait_document(
    text: str = "true",
    *,
    poll: bool = False,
    nested: bool = False,
    optional: bool = False,
) -> GraphDocument:
    external = "observation" if poll else "payload"
    nodes = {
        "producer": producer(schema=SCHEMA),
        "wait": wait(
            text.replace("external", external),
            poll=poll,
            inputs={
                "data": {
                    "type": "StructuredOutput",
                    "from": "producer",
                    "output": "value",
                    "optional": optional,
                }
            },
            source={"file": "wait.py", "line": 7},
        ),
    }
    return document_for(
        params=SCHEMA,
        contracts=BUILTINS,
        nodes={"timer": node()} if nested else nodes,
        bodies={"unused": {"nodes": nodes}} if nested else None,
    )
