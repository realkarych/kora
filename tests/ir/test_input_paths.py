import json

import pytest

from kora.ir import export_ir, validate_expression_syntax, validate_input_paths
from tests.ir.document_cases import NODE_CASES
from tests.ir.expression_syntax_cases import FieldPath, set_field
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.input_path_cases import (
    PATH_SITES,
    binding_document,
    binding_node,
    bindings,
    shared_bindings,
)


@pytest.mark.parametrize(("kind", "suffix", "code"), PATH_SITES)
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(
    ("text", "valid"),
    [
        ("inputs.data", True),
        ('inputs["data"]', True),
        ("inputs['d\\u0061ta']", True),
        ("inputs.data[0].unknown", True),
        ("other.data", False),
        ("Inputs.data", False),
        ("inputs.Data", False),
        ("inputs.missing", False),
        ("inputs", False),
        ("inputs[0]", False),
        ("inputs['']", False),
    ],
)
def test_input_paths_bind_to_current_node(
    kind: str, suffix: FieldPath, code: str, nested: bool, text: str, valid: bool
) -> None:
    graph = binding_document(text, kind, suffix, nested=nested)
    before = export_ir(graph)
    prefix = ("graph", "subgraphs", "unused") if nested else ("graph",)

    result = validate_input_paths(graph)

    assert result.input_paths_valid is valid
    assert result.paths_checked == result.bindings_checked == 1
    assert result.paths_unresolved == 0
    assert result.nodes_checked == 1 + int(nested)
    assert result.definitions_checked == int(nested)
    assert export_ir(graph) == before
    assert len(result.diagnostics.errors) == int(not valid)
    for error in result.diagnostics.errors:
        assert error.code == code
        assert error.message == "The path must select a declared input of this node."
        assert error.path == (*prefix, "nodes", "subject", *suffix)
        assert error.node == "subject"
        assert error.offset is None
        assert error.location is not None
        assert error.location.file == "node.py"


@pytest.mark.parametrize("visible_on", ["body", "caller", "sibling"])
@pytest.mark.parametrize("own_inputs", [None, {}])
def test_input_paths_do_not_inherit_inputs(
    visible_on: str, own_inputs: dict[str, object] | None
) -> None:
    subject = binding_node()
    subject.pop("inputs")
    if own_inputs is not None:
        subject["inputs"] = own_inputs
    declaration = {"data": {"type": "StructuredOutput"}}
    body: dict[str, object] = {"nodes": {"subject": subject}}
    caller = node("subgraph", config={"body": "body"})
    if visible_on == "body":
        body["inputs"] = declaration
    elif visible_on == "caller":
        caller["inputs"] = declaration
    else:
        body["nodes"] = {"subject": subject, "sibling": node(inputs=declaration)}
    graph = document_for(nodes={"caller": caller}, bodies={"body": body})

    result = validate_input_paths(graph)

    assert not result.input_paths_valid
    assert result.paths_checked == result.bindings_checked == 1
    assert result.paths_unresolved == 0
    assert len(result.diagnostics.errors) == 1
    assert result.diagnostics.errors[0].path == (
        "graph",
        "subgraphs",
        "body",
        "nodes",
        "subject",
        "config",
        "context",
    )


@pytest.mark.parametrize("optional", [False, True])
def test_input_paths_preserve_scope_and_deferred_semantics(optional: bool) -> None:
    subject = binding_node("inputs.data[0].absent")
    subject["inputs"] = {
        "data": {
            "type": "UnknownType",
            "from": "absent",
            "output": "absent",
            "optional": optional,
            "path": "not a relative path",
        }
    }
    set_field(subject, ("config", "approve_when"), "not valid CEL &&")
    graph = document_for(
        nodes={"subject": subject},
        target={"key": {"from": "subject", "output": "response", "path": "bad path"}},
    )
    before = export_ir(graph)

    result = validate_input_paths(graph)

    assert result.input_paths_valid
    assert result.paths_checked == result.bindings_checked == 1
    assert result.paths_unresolved == 0
    assert export_ir(graph) == before


def test_input_paths_ignore_absent_fields_and_opaque_strings() -> None:
    nodes = {kind: node(kind) for kind in NODE_CASES}
    set_field(nodes["agent"], ("config", "prompt"), "inputs.missing.")
    set_field(nodes["command"], ("config", "cmd"), ["inputs.missing."])
    graph = document_for(nodes=nodes)

    result = validate_input_paths(graph)

    assert result.input_paths_valid
    assert result.nodes_checked == len(NODE_CASES)
    assert (
        result.paths_checked == result.bindings_checked == result.paths_unresolved == 0
    )


@pytest.mark.parametrize(
    ("text", "syntax_code"),
    [("", "IR011"), ("inputs.data.", "IR011"), ("inputs.data" + ".x" * 32, "IR027")],
)
def test_input_paths_do_not_duplicate_syntax_errors(
    text: str, syntax_code: str
) -> None:
    subject = binding_node(text)
    set_field(subject, ("config", "binds", "vcs_ref"), "inputs.missing")
    graph = document_for(nodes={"subject": subject})

    result = validate_input_paths(graph)
    syntax = validate_expression_syntax(graph)

    assert not result.input_paths_valid
    assert result.paths_checked == 2
    assert result.bindings_checked == result.paths_unresolved == 1
    assert [error.code for error in result.diagnostics.errors] == ["IR005"]
    assert [error.code for error in syntax.diagnostics.errors] == [syntax_code]


def test_input_paths_keep_each_gate_digest_index() -> None:
    subject = binding_node()
    set_field(
        subject,
        ("config", "binds", "gate_digests"),
        ["inputs.data", "inputs.missing", "elsewhere.data", "inputs.data."],
    )
    graph = document_for(nodes={"subject": subject})

    result = validate_input_paths(graph)

    assert not result.input_paths_valid
    assert result.paths_checked == 5
    assert result.bindings_checked == 4
    assert result.paths_unresolved == 1
    assert [error.code for error in result.diagnostics.errors] == ["IR005", "IR005"]
    assert [error.path for error in result.diagnostics.errors] == [
        ("graph", "nodes", "subject", "config", "binds", "gate_digests", index)
        for index in (1, 2)
    ]


@pytest.mark.parametrize(
    "source", [None, "/private/secret.py", "https://host/secret", "x" * 1025]
)
def test_input_paths_use_only_safe_node_source(source: str | None) -> None:
    subject = binding_node("inputs.missing")
    subject.pop("source")
    if source is not None:
        subject["source"] = {"file": source, "line": 1}
    graph = document_for(
        bodies={
            "unused": {
                "nodes": {"subject": subject},
                "source": {"file": "body.py", "line": 2},
            }
        },
        source={"file": "graph.py", "line": 3},
    )

    result = validate_input_paths(graph)

    assert len(result.diagnostics.errors) == 1
    assert result.diagnostics.errors[0].location is None


@pytest.mark.parametrize("byte_pressure", [False, True])
def test_input_path_diagnostics_are_bounded_and_safe(byte_pressure: bool) -> None:
    marker = "private_path_value"
    if byte_pressure:
        nodes: dict[str, dict[str, object]] = {}
        for i in range(3000):
            subject = binding_node(f"inputs.{marker}")
            subject["source"] = {"file": "s" * 1000, "line": 1}
            nodes["n" * 60 + str(i)] = subject
        graph = document_for(nodes=nodes)
    else:
        graph = bindings(3000, f"inputs.{marker}")

    result = validate_input_paths(graph)
    payload = json.dumps(
        {
            "errors": [
                error.model_dump(mode="json") for error in result.diagnostics.errors
            ],
            "warnings": [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()

    assert not result.input_paths_valid
    assert result.paths_checked == result.bindings_checked == 3000
    assert result.paths_unresolved == 0
    assert len(result.diagnostics.errors) + result.diagnostics.errors_omitted == 3000
    assert (
        len(result.diagnostics.errors) < 1000
        if byte_pressure
        else len(result.diagnostics.errors) == 1000
    )
    assert len(payload) <= 1_048_576
    assert marker.encode() not in payload
    assert all(error.offset is None for error in result.diagnostics.errors)


def test_input_paths_visit_shared_body_once() -> None:
    graph = shared_bindings(1000)

    result = validate_input_paths(graph)

    assert result.input_paths_valid
    assert result.nodes_checked == 1001
    assert (
        result.definitions_checked
        == result.paths_checked
        == result.bindings_checked
        == 1
    )
    assert result.paths_unresolved == 0
