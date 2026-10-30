from kora.ir import GraphDocument
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.type_declaration_cases import custom


def reference(
    expected: str, source: str = "producer", /, **fields: object
) -> dict[str, object]:
    return {"type": expected, "from": source, "output": "value", **fields}


def edge_nodes(
    produced: str, expected: str, **fields: object
) -> dict[str, dict[str, object]]:
    return {
        "producer": node(outputs={"value": {"type": produced}}),
        "consumer": node(inputs={"value": reference(expected, **fields)}),
    }


def edges(count: int, invalid: bool = False) -> GraphDocument:
    return document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={
            "producer": node(outputs={"value": {"type": "example/A"}}),
            **{
                f"n{index}": node(
                    inputs={"value": reference("example/B" if invalid else "example/A")}
                )
                for index in range(count)
            },
        },
    )


def ancestry_edges(depth: int, count: int, invalid: bool = False) -> GraphDocument:
    return document_for(
        contracts={
            f"example/T{index}": custom(
                f"example/T{index + 1}"
                if index + 1 < depth
                else ("example/Missing" if invalid else None)
            )
            for index in range(depth)
        },
        nodes={
            "producer": node(outputs={"value": {"type": "example/T0"}}),
            **{
                f"n{index}": node(
                    inputs={"value": reference(f"example/T{index % depth}")}
                )
                for index in range(count)
            },
        },
    )


def siblings(count: int) -> GraphDocument:
    nodes: dict[str, dict[str, object]] = {}
    for index in range(count):
        nodes[f"p{index}"] = node(outputs={"value": {"type": f"example/T{index}"}})
        nodes[f"c{index}"] = node(
            inputs={"value": reference("example/Base", f"p{index}")}
        )
    return document_for(
        contracts={
            "example/Base": custom(),
            **{f"example/T{index}": custom("example/Base") for index in range(count)},
        },
        nodes=nodes,
    )


def shared_body(count: int) -> GraphDocument:
    return document_for(
        contracts={"example/A": custom()},
        nodes={
            f"n{index}": node("subgraph", config={"body": "body"})
            for index in range(count)
        },
        bodies={"body": {"nodes": edge_nodes("example/A", "example/A")}},
    )
