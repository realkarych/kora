from kora.ir import GraphDocument
from tests.ir.edge_type_cases import nested_type
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.type_declaration_cases import custom


def interface_body(
    incoming: str = "example/A",
    exported: str = "example/A",
    produced: str | None = None,
) -> dict[str, object]:
    return {
        "nodes": {"producer": node(outputs={"value": {"type": produced or exported}})},
        "inputs": {"incoming": {"type": incoming}},
        "outputs": {"value": {"type": exported, "from": "producer", "output": "value"}},
    }


def interface_wrapper(
    incoming: str = "example/A", exported: str = "example/A", **fields: object
) -> dict[str, object]:
    return node(
        "subgraph",
        config={"body": "body"},
        inputs={"incoming": {"type": incoming, "from": "$params"}},
        outputs={"value": {"type": exported}},
        **fields,
    )


def interfaces(count: int, invalid: bool = False, *, depth: int = 0) -> GraphDocument:
    type_name = nested_type("example/A", depth)
    other = nested_type("example/B" if invalid else "example/A", depth)
    return document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={
            f"n{index}": interface_wrapper(other, type_name) for index in range(count)
        },
        bodies={"body": interface_body(type_name, type_name)},
    )


def deep_ancestry_interfaces(
    depth: int, count: int, invalid: bool = False
) -> GraphDocument:
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
            f"n{index}": interface_wrapper("example/T0", "example/T0")
            for index in range(count)
        },
        bodies={"body": interface_body(f"example/T{depth - 1}", "example/T0")},
    )


def unused_interfaces(count: int) -> GraphDocument:
    return document_for(
        contracts={"example/A": custom()},
        bodies={
            "body": interface_body(),
            **{
                f"unused{index}": {"nodes": {"call": interface_wrapper()}}
                for index in range(count)
            },
        },
    )
