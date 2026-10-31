from kora.ir import GraphDocument
from tests.ir.graph_link_cases import node


def chain_nodes() -> dict[str, dict[str, object]]:
    return {
        "agent": node(
            "agent",
            outputs={
                "session": {"type": "AgentSession"},
                "change": {"type": "VcsRef"},
            },
        ),
        "gate": node(
            "command",
            inputs={"change": {"type": "VcsRef", "from": "agent", "output": "change"}},
        ),
        "approve": node(
            "human_input",
            inputs={
                "change": {"type": "VcsRef", "from": "agent", "output": "change"},
                "gate": {"type": "CommandResult", "from": "gate", "output": "result"},
            },
        ),
    }


def chain_document(
    nodes: dict[str, dict[str, object]] | None = None,
    runtime_fields: dict[str, object] | None = None,
    **graph_fields: object,
) -> GraphDocument:
    return GraphDocument.model_validate(
        {
            "ir_version": "1.0",
            "type_contracts": {
                name: {"builtin_revision": "1"}
                for name in (
                    "AgentSession",
                    "CommandResult",
                    "StructuredOutput",
                    "VcsRef",
                )
            },
            "graph": {
                "id": "graph",
                "namespace": "example",
                "nodes": chain_nodes() if nodes is None else nodes,
                **graph_fields,
            },
            "runtime": {
                "runtime": "local",
                "vcs": "git",
                **({} if runtime_fields is None else runtime_fields),
            },
        }
    )
