from kora.ir import GraphDocument
from tests.ir.expression_syntax_cases import FieldPath, set_field
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for

PATH_SITES: tuple[tuple[str, FieldPath, str], ...] = (
    ("agent", ("config", "workspace", "from_ref"), "IR002"),
    ("command", ("config", "workspace", "from_ref"), "IR002"),
    ("human_input", ("config", "context"), "IR002"),
    ("human_input", ("config", "binds", "vcs_ref"), "IR005"),
    ("human_input", ("config", "binds", "gate_digests", 0), "IR005"),
)


def binding_node(
    text: str = "inputs.data",
    kind: str = "human_input",
    path: FieldPath = ("config", "context"),
) -> dict[str, object]:
    value = node(
        kind,
        inputs={"data": {"type": "StructuredOutput"}},
        source={"file": "node.py", "line": 7},
    )
    set_field(value, path, text)
    return value


def binding_document(
    text: str,
    kind: str = "human_input",
    path: FieldPath = ("config", "context"),
    *,
    nested: bool = False,
) -> GraphDocument:
    nodes = {"subject": binding_node(text, kind, path)}
    return document_for(
        nodes={"timer": node()} if nested else nodes,
        bodies={"unused": {"nodes": nodes}} if nested else None,
    )


def bindings(count: int, text: str = "inputs.data") -> GraphDocument:
    return document_for(nodes={f"n{i}": binding_node(text) for i in range(count)})


def shared_bindings(count: int) -> GraphDocument:
    return document_for(
        nodes={
            f"n{i}": node("subgraph", config={"body": "body"}) for i in range(count)
        },
        bodies={"body": {"nodes": {"subject": binding_node()}}},
    )
