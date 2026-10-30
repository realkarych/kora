from kora.ir import GraphDocument
from kora.ir.json import JsonObject
from tests.ir.graph_link_cases import node

OUTPUT_ROLES: dict[str, tuple[str, str]] = {
    "agent": ("session", "AgentSession"),
    "command": ("result", "CommandResult"),
    "human_input": ("response", "StructuredOutput"),
    "wait_event": ("event", "StructuredOutput"),
}


def schema_node(kind: str, schema: JsonObject) -> dict[str, object]:
    name, type_name = OUTPUT_ROLES.get(kind, ("value", "StructuredOutput"))
    return node(kind, outputs={name: {"type": type_name, "schema": schema}})


def document_for(
    params: JsonObject | None = None,
    nodes: dict[str, dict[str, object]] | None = None,
    bodies: dict[str, dict[str, object]] | None = None,
    contracts: dict[str, dict[str, object]] | None = None,
    **graph_fields: object,
) -> GraphDocument:
    return GraphDocument.model_validate(
        {
            "ir_version": "1.0",
            "type_contracts": {} if contracts is None else contracts,
            "graph": {
                "id": "graph",
                "namespace": "example",
                "nodes": {"timer": node()} if nodes is None else nodes,
                "subgraphs": {} if bodies is None else bodies,
                **({} if params is None else {"params": params}),
                **graph_fields,
            },
            "runtime": {"runtime": "local", "vcs": "git"},
        }
    )


def many_roots(count: int) -> GraphDocument:
    return document_for(
        nodes={f"n{index}": schema_node("timer", {}) for index in range(count)}
    )


def shared_body(count: int) -> GraphDocument:
    return document_for(
        nodes={
            f"n{index}": node("subgraph", config={"body": "body"})
            for index in range(count)
        },
        bodies={"body": {"nodes": {"timer": schema_node("timer", {})}}},
    )
