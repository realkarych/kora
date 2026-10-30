from kora.ir import GraphDocument
from tests.ir.graph_link_cases import node


def custom(parent: str | None = None) -> dict[str, object]:
    return {
        "schema": {"$schema": "https://json-schema.org/draft/2020-12/schema"},
        "parent": parent,
        "behavior": {
            "validation": "kora.json-schema-2020-12.v1",
            "digest_fields": "kora.json-payload.v1",
            "summary": "kora.json-payload.v1",
        },
    }


def model(
    contracts: dict[str, dict[str, object]] | None = None,
    nodes: dict[str, dict[str, object]] | None = None,
    bodies: dict[str, dict[str, object]] | None = None,
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
            },
            "runtime": {"runtime": "local", "vcs": "git"},
        }
    )


def declarations(count: int, invalid: bool = False, depth: int = 0) -> GraphDocument:
    name = "List[" * depth + "example/Value" + "]" * depth
    return model(
        {} if invalid else {"example/Value": custom()},
        {f"n{index}": node(inputs={"value": {"type": name}}) for index in range(count)},
    )


def parent_chain(count: int, cyclic: bool = False) -> GraphDocument:
    return model(
        {
            f"example/T{index}": custom(
                f"example/T{index + 1}"
                if index + 1 < count
                else ("example/T0" if cyclic else None)
            )
            for index in range(count)
        }
    )
