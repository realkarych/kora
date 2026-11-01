from kora.ir import GraphDocument
from kora.ir.json import JsonObject
from tests.ir.approval_default_cases import decision_form, gate, saved_form
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for


def shared_predicates(
    count: int,
    depth: int,
    predicate: str = "response.decision == 'approve'",
    invalid: bool = False,
    nested: bool = False,
) -> GraphDocument:
    schema: JsonObject = decision_form(
        {"enum": ["reject"] if invalid else ["approve", "reject"]}
    )
    for _ in range(depth):
        schema = {"allOf": [schema]}
    nodes = {
        f"n{index}": gate(
            {"$ref": "urn:kora:artifact:example:Form"},
            predicate,
            source={"file": "node.py", "line": 1},
        )
        for index in range(count)
    }
    return document_for(
        contracts={"example/Form": saved_form(schema)},
        nodes={"root": node()} if nested else nodes,
        bodies={"unused": {"nodes": nodes}} if nested else None,
    )
