from kora.ir import GraphDocument
from tests.ir.edge_type_cases import nested_type, reference
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.type_declaration_cases import custom


def export_body(produced: str, expected: str, count: int = 1) -> dict[str, object]:
    return {
        "nodes": {"producer": node(outputs={"value": {"type": produced}})},
        "outputs": {f"e{index}": reference(expected) for index in range(count)},
    }


def exports(count: int, invalid: bool = False, *, list_depth: int = 0) -> GraphDocument:
    return document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        bodies={
            "body": export_body(
                nested_type("example/A", list_depth),
                nested_type("example/B" if invalid else "example/A", list_depth),
                count,
            )
        },
    )


def ancestry_exports(depth: int, count: int, invalid: bool = False) -> GraphDocument:
    body = export_body("example/T0", "example/T0")
    body["outputs"] = {
        f"e{index}": reference(f"example/T{index % depth}") for index in range(count)
    }
    return document_for(
        contracts={
            f"example/T{index}": custom(
                f"example/T{index + 1}"
                if index + 1 < depth
                else ("example/Missing" if invalid else None)
            )
            for index in range(depth)
        },
        bodies={"body": body},
    )


def shared_exports(count: int) -> GraphDocument:
    return document_for(
        contracts={"example/A": custom()},
        nodes={
            f"n{index}": node("subgraph", config={"body": "body"})
            for index in range(count)
        },
        bodies={"body": export_body("example/A", "example/A")},
    )


def unused_exports(count: int) -> GraphDocument:
    return document_for(
        contracts={"example/A": custom()},
        bodies={
            f"b{index}": export_body("example/A", "example/A") for index in range(count)
        },
    )
