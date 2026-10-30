from typing import cast

from kora.ir import GraphDocument
from tests.ir.document_cases import document
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for

FieldPath = tuple[str | int, ...]


def set_field(root: object, path: FieldPath, value: object) -> None:
    current = root
    for position, step in enumerate(path):
        final = position == len(path) - 1
        child: object = (
            value if final else ([] if isinstance(path[position + 1], int) else {})
        )
        if isinstance(step, str):
            assert isinstance(current, dict)
            mapping = cast("dict[str, object]", current)
            if final:
                mapping[step] = value
            current = mapping.setdefault(step, child)
        else:
            assert isinstance(current, list)
            sequence = cast("list[object]", current)
            while len(sequence) <= step:
                sequence.append(child)
            if final:
                sequence[step] = value
            current = sequence[step]


def field_document(
    kind: str, path: FieldPath, text: str, *, nested: bool = False
) -> GraphDocument:
    value = node(kind)
    if path[:2] == ("config", "poll"):
        value["config"] = {
            "poll": {"sensor": "http", "until": "true"},
            "timeout": "0s",
            "on_expiry": "skip",
        }
    if path[:1] == ("inputs",):
        value["inputs"] = {"data": {"type": "StructuredOutput"}}
    set_field(value, path, text)
    value["source"] = {"file": "node.py", "line": 7}
    nodes = {"subject": value}
    return document_for(
        nodes={"timer": node()} if nested else nodes,
        bodies={"unused": {"nodes": nodes}} if nested else None,
    )


def root_field_document(path: FieldPath, text: str) -> GraphDocument:
    value = document()
    set_field(value, ("graph", "source"), {"file": "graph.py", "line": 3})
    set_field(
        value,
        ("graph", "subgraphs", "unused"),
        {"nodes": {"timer": node()}, "source": {"file": "body.py", "line": 5}},
    )
    set_field(
        value,
        ("graph", "triggers"),
        [
            {"kind": "manual"},
            {
                "kind": "sensor",
                "poll": {"sensor": "http", "until": "true"},
                "interval": "0s",
            },
        ],
    )
    set_field(
        value,
        ("graph", "target"),
        {"key": {"from": "timer", "output": "value", "path": "value"}},
    )
    set_field(value, path, text)
    return GraphDocument.model_validate(value)


def predicates(count: int, text: str = "true") -> GraphDocument:
    return document_for(
        nodes={f"n{index}": node(retry={"predicate": text}) for index in range(count)}
    )


def paths(count: int) -> GraphDocument:
    return document_for(
        nodes={
            f"n{index}": node(
                inputs={"data": {"type": "StructuredOutput", "path": "value.items[0]"}}
            )
            for index in range(count)
        }
    )


def shared_body(count: int) -> GraphDocument:
    return document_for(
        nodes={
            f"n{index}": node("subgraph", config={"body": "body"})
            for index in range(count)
        },
        bodies={"body": {"nodes": {"timer": node(retry={"predicate": "true"})}}},
    )
