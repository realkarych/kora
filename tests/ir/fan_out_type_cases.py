from kora.ir import GraphDocument
from tests.ir.edge_type_cases import nested_type, reference
from tests.ir.export_type_cases import export_body
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.type_declaration_cases import custom


def fan_out_node(
    mapped: str = "List[example/A]",
    collected: str = "List[example/A]",
    count: int = 1,
    **fields: object,
) -> dict[str, object]:
    return node(
        "fan_out",
        **{
            "config": {"body": "body", "over": "items", "max_parallel": 2},
            "inputs": {"items": reference(mapped, "$params")},
            "outputs": {
                f"c{index}": {"type": collected, "collect": "e0"}
                for index in range(count)
            },
            **fields,
        },
    )


def fan_outs(
    count: int,
    invalid: bool = False,
    *,
    list_depth: int = 0,
    output_count: int = 1,
) -> GraphDocument:
    exported = nested_type("example/A", list_depth)
    collected = nested_type("example/B" if invalid else "example/A", list_depth + 1)
    return document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={
            f"n{index}": fan_out_node(collected=collected, count=output_count)
            for index in range(count)
        },
        bodies={"body": export_body(exported, exported)},
    )


def ancestry_fan_outs(depth: int, count: int, invalid: bool = False) -> GraphDocument:
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
            f"n{index}": fan_out_node(
                "List[example/T0]", f"List[example/T{index % depth}]"
            )
            for index in range(count)
        },
        bodies={"body": export_body("example/T0", "example/T0")},
    )


def stored_fan_outs(count: int, *, shared: bool) -> GraphDocument:
    return document_for(
        contracts={"example/A": custom()},
        nodes={
            f"n{index}": node("subgraph", config={"body": "stored0"})
            for index in range(count if shared else 1)
        },
        bodies={
            "body": export_body("example/A", "example/A"),
            **{
                f"stored{index}": {"nodes": {"fan": fan_out_node()}}
                for index in range(1 if shared else count)
            },
        },
    )
