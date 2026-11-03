from kora.ir import GraphDocument
from kora.ir.json import JsonObject
from tests.ir.approval_default_cases import gate
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for

BUILTINS: dict[str, dict[str, object]] = {
    name: {"builtin_revision": "1"}
    for name in ("VcsRef", "CommandResult", "AgentSession", "StructuredOutput")
}


def unavailable_inputs() -> dict[str, object]:
    return {
        "unavailable": {
            "type": "StructuredOutput",
            "from": "missing",
            "output": "value",
        }
    }


def producer(
    type_name: str = "StructuredOutput", schema: JsonObject | None = None
) -> dict[str, object]:
    if type_name == "CommandResult":
        return node(
            "command",
            outputs={
                "result": {
                    "type": type_name,
                    **({} if schema is None else {"schema": schema}),
                }
            },
        )
    return node(
        "command",
        outputs={
            "result": {"type": "CommandResult"},
            "value": {
                "type": type_name,
                **({} if schema is None else {"schema": schema}),
            },
        },
    )


def direct_document(
    predicate: str,
    source: dict[str, object] | None = None,
    type_name: str = "StructuredOutput",
    output: str = "value",
    optional: bool = False,
    nested: bool = False,
    contracts: dict[str, dict[str, object]] | None = None,
) -> GraphDocument:
    if type_name == "CommandResult" and output == "value":
        output = "result"
    nodes = {
        "producer": producer() if source is None else source,
        "gate": gate(
            None,
            predicate,
            inputs={
                "data": {
                    "type": type_name,
                    "from": "producer",
                    "output": output,
                    "optional": optional,
                }
            },
        ),
    }
    return document_for(
        contracts=BUILTINS if contracts is None else contracts,
        nodes={"root": node()} if nested else nodes,
        bodies={"unused": {"nodes": nodes}} if nested else None,
        params={"properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
    )
