from copy import deepcopy

from kora.ir import GraphDocument
from tests.ir.document_cases import NODE_CASES


def node(kind: str = "timer", **fields: object) -> dict[str, object]:
    return {"kind": kind, **deepcopy(NODE_CASES[kind]), **fields}


def model(
    nodes: dict[str, dict[str, object]],
    definitions: dict[str, dict[str, object]] | None = None,
    **graph_fields: object,
) -> GraphDocument:
    return GraphDocument.model_validate(
        {
            "ir_version": "1.0",
            "type_contracts": {},
            "graph": {
                "id": "graph",
                "namespace": "example",
                "nodes": nodes,
                "subgraphs": definitions or {},
                **graph_fields,
            },
            "runtime": {"runtime": "local", "vcs": "git"},
        }
    )


def ref(source: str, output: str = "value", **fields: object) -> dict[str, object]:
    return {"type": "StructuredOutput", "from": source, "output": output, **fields}


def chain(count: int) -> GraphDocument:
    return model(
        {
            f"n{index}": node(depends_on=[] if index == 0 else [f"n{index - 1}"])
            for index in range(count)
        }
    )


def dense_faults(count: int, width: int = 3) -> GraphDocument:
    return model(
        {
            f"n{index}": node(
                inputs={f"i{offset}": ref("absent") for offset in range(width)}
            )
            for index in range(count)
        }
    )


def definition_chain(count: int, recursive: bool = False) -> GraphDocument:
    definitions: dict[str, dict[str, object]] = {
        f"b{index}": {
            "nodes": {"call": node("subgraph", config={"body": f"b{index + 1}"})}
        }
        for index in range(count - 1)
    }
    definitions[f"b{count - 1}"] = {
        "nodes": {
            "call": node("subgraph", config={"body": "b0"}) if recursive else node()
        }
    }
    return model({"start": node("subgraph", config={"body": "b0"})}, definitions)
