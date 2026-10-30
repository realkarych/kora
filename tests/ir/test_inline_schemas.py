import json

import pytest
from pydantic import MISSING

from kora.ir import export_ir, validate_inline_schemas
from kora.ir.contracts import SCHEMA_DIALECT
from kora.ir.json import JsonObject, JsonValue
from tests.ir.document_cases import NODE_CASES
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import (
    OUTPUT_ROLES,
    document_for,
    schema_node,
    shared_body,
)
from tests.ir.type_schema_cases import nested_schema


@pytest.mark.parametrize("kind", NODE_CASES)
@pytest.mark.parametrize("nested", [False, True])
def test_inline_schemas_check_every_node_output(kind: str, nested: bool) -> None:
    candidate = schema_node(kind, {"type": "invalid"})
    candidate["source"] = {"file": "node.py", "line": 17}
    nodes = {"candidate": candidate}
    document = document_for(
        nodes={"root": node()} if nested else nodes,
        bodies={"unused": {"nodes": nodes}} if nested else None,
        source={"file": "graph.py", "line": 1},
    )
    prefix = ("graph", "subgraphs", "unused") if nested else ("graph",)
    output = OUTPUT_ROLES.get(kind, ("value", "StructuredOutput"))[0]

    result = validate_inline_schemas(document)

    assert not result.inline_schemas_valid
    assert result.nodes_checked == (2 if nested else 1)
    assert result.definitions_checked == int(nested)
    assert result.inline_schemas_checked == result.schemas_checked == 1
    (error,) = result.diagnostics.errors
    assert error.code == "IR022"
    assert error.path == (
        *prefix,
        "nodes",
        "candidate",
        "outputs",
        output,
        "schema",
        "type",
    )
    assert error.node == "candidate"
    assert error.location is not None
    assert (error.location.file, error.location.line) == ("node.py", 17)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(
    ("kind", "field"), [("agent", "output_schema"), ("human_input", "form")]
)
def test_inline_schemas_check_node_configuration(
    kind: str, field: str, nested: bool
) -> None:
    candidate = node(kind)
    config = candidate["config"]
    assert isinstance(config, dict)
    candidate["config"] = {**config, field: {"$ref": "#/missing"}}
    candidate["source"] = {"file": "node.py", "line": 11}
    nodes = {"candidate": candidate}
    document = document_for(
        nodes={"root": node()} if nested else nodes,
        bodies={"unused": {"nodes": nodes}} if nested else None,
    )
    prefix = ("graph", "subgraphs", "unused") if nested else ("graph",)

    result = validate_inline_schemas(document)

    (error,) = result.diagnostics.errors
    assert error.path == (*prefix, "nodes", "candidate", "config", field, "$ref")
    assert error.node == "candidate"
    assert error.location is not None
    assert error.location.file == "node.py"
    assert result.inline_schemas_checked == result.references_checked == 1


def test_inline_schemas_attribute_graph_and_body_fields_to_their_owner() -> None:
    document = document_for(
        {"type": "invalid"},
        bodies={
            "unused": {
                "nodes": {"timer": node()},
                "inputs": {
                    "data": {"type": "StructuredOutput", "schema": {"type": "invalid"}}
                },
                "source": {"file": "body.py", "line": 2},
            }
        },
        source={"file": "graph.py", "line": 1},
    )

    result = validate_inline_schemas(document)

    assert result.inline_schemas_checked == 2
    assert [
        (error.path, error.node, error.location.file if error.location else None)
        for error in result.diagnostics.errors
    ] == [
        (("graph", "params", "type"), None, "graph.py"),
        (
            ("graph", "subgraphs", "unused", "inputs", "data", "schema", "type"),
            None,
            "body.py",
        ),
    ]


@pytest.mark.parametrize(
    "label", ["/private/schema.py", "https://example.invalid/private", "x" * 1025]
)
def test_inline_schemas_omit_unsafe_source_without_using_ancestor(label: str) -> None:
    candidate = schema_node("timer", {"type": "invalid"})
    candidate["source"] = {"file": label, "line": 1}
    document = document_for(
        nodes={"candidate": candidate}, source={"file": "graph.py", "line": 2}
    )

    result = validate_inline_schemas(document)

    (error,) = result.diagnostics.errors
    assert error.location is None
    assert error.node == "candidate"


@pytest.mark.parametrize("authored", [False, True])
def test_inline_schemas_preserve_absence_and_empty_maps(authored: bool) -> None:
    config: dict[str, object] = {
        "channel": "cli",
        "timeout": "0s",
        "on_expiry": "fail",
        **({"form": {}} if authored else {}),
    }
    candidate = node(
        "human_input",
        config=config,
    )
    document = document_for({} if authored else None, nodes={"gate": candidate})
    before = export_ir(document)

    result = validate_inline_schemas(document)

    assert result.inline_schemas_valid
    assert result.inline_schemas_checked == (2 if authored else 0)
    assert export_ir(document) == before
    assert (document.graph.params is MISSING) is not authored


@pytest.mark.parametrize(
    "dialect", [SCHEMA_DIALECT, "https://json-schema.org/draft-07/schema", 1, None]
)
def test_inline_schemas_validate_explicit_dialect(dialect: JsonValue) -> None:
    document = document_for({"$schema": dialect})

    result = validate_inline_schemas(document)

    assert result.inline_schemas_valid is (dialect == SCHEMA_DIALECT)
    assert all(
        error.path == ("graph", "params", "$schema")
        for error in result.diagnostics.errors
    )


def test_inline_schemas_ignore_payload_metadata_and_unchecked_approval_semantics() -> (
    None
):
    payload: JsonObject = {"$ref": "https://example.invalid/private", "type": "invalid"}
    extensions: dict[str, object] = {"x-schema": payload}
    candidate = node(
        "human_input",
        config={
            "channel": "cli",
            "timeout": "0s",
            "on_expiry": "fail",
            "form": {"type": "string"},
        },
        outputs={
            "response": {"type": "StructuredOutput", "schema": {"type": "integer"}}
        },
        **extensions,
    )
    document = document_for(
        {
            "default": dict(payload),
            "const": dict(payload),
            "examples": [dict(payload)],
            "extension": dict(payload),
        },
        nodes={"gate": candidate},
        bodies=None,
        contracts=None,
        triggers=[{"kind": "manual", "params": payload}],
        **extensions,
    )

    result = validate_inline_schemas(document)

    assert result.inline_schemas_valid
    assert result.inline_schemas_checked == result.schemas_checked == 3
    assert result.references_checked == 0


def test_inline_schemas_visit_stored_body_once_without_call_expansion() -> None:
    document = shared_body(1000)

    result = validate_inline_schemas(document)

    assert result.inline_schemas_valid
    assert result.nodes_checked == 1001
    assert result.definitions_checked == 1
    assert result.inline_schemas_checked == result.schemas_checked == 1


def test_inline_schemas_bound_deep_independent_faults_and_mask_authored_names() -> None:
    marker = "private_schema_content"
    schema: JsonObject = {
        "properties": {
            marker * 100: nested_schema(
                1200, {"type": "invalid", "pattern": "(" * 1200}
            )
        },
        "type": ["invalid" for _ in range(3000)],
    }
    document = document_for(schema)

    result = validate_inline_schemas(document)

    assert not result.inline_schemas_valid
    assert result.schemas_checked == 1202
    assert len(result.diagnostics.errors) + result.diagnostics.errors_omitted == 3002
    assert len(result.diagnostics.errors) <= 1000
    assert result.diagnostics.errors_omitted > 0
    assert all(
        error.path is not None and len(error.path) <= 64
        for error in result.diagnostics.errors
    )
    rendered = json.dumps(
        [error.model_dump(mode="json") for error in result.diagnostics.errors]
    )
    assert marker not in rendered
    assert "<field>" in rendered
    assert len(rendered.encode()) <= 1_048_576
    assert {error.message for error in result.diagnostics.errors} == {
        "The inline schema or reference is invalid."
    }
