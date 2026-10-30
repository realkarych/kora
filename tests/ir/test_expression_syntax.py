import json

import pytest
from pydantic import ValidationError

from kora.ir import GraphDocument, export_ir, validate_expression_syntax
from tests.ir.document_cases import NODE_CASES, document
from tests.ir.expression_syntax_cases import (
    FieldPath,
    field_document,
    predicates,
    root_field_document,
    set_field,
    shared_body,
)
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for

EXPRESSION_SITES: tuple[tuple[str, FieldPath, int], ...] = (
    ("branch", ("config", "cases", 0, "when"), 1),
    ("branch", ("config", "cases", 0, "reason"), 2),
    ("branch", ("config", "else_reason"), 2),
    ("human_input", ("config", "approve_when"), 1),
    ("wait_event", ("config", "event", "match"), 1),
    ("wait_event", ("config", "poll", "until"), 1),
    *(
        (kind, ("retry", "predicate"), 2 if kind == "branch" else 1)
        for kind in NODE_CASES
    ),
)
PATH_SITES: tuple[tuple[str, FieldPath], ...] = (
    ("agent", ("config", "workspace", "from_ref")),
    ("command", ("config", "workspace", "from_ref")),
    ("human_input", ("config", "context")),
    ("human_input", ("config", "binds", "vcs_ref")),
    ("human_input", ("config", "binds", "gate_digests", 0)),
    *((kind, ("inputs", "data", "path")) for kind in NODE_CASES),
)


@pytest.mark.parametrize(("kind", "suffix", "count"), EXPRESSION_SITES)
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("invalid", [False, True])
def test_expression_syntax_covers_operational_fields(
    kind: str, suffix: FieldPath, count: int, nested: bool, invalid: bool
) -> None:
    source = "params.ready &&" if invalid else "params.ready"
    graph = field_document(kind, suffix, source, nested=nested)
    before = export_ir(graph)
    prefix = ("graph", "subgraphs", "unused") if nested else ("graph",)

    result = validate_expression_syntax(graph)

    assert result.syntax_valid is not invalid
    assert result.expressions_checked == count
    assert result.paths_checked == result.triggers_checked == 0
    assert result.nodes_checked == 1 + int(nested)
    assert result.definitions_checked == int(nested)
    assert export_ir(graph) == before
    assert len(result.diagnostics.errors) == int(invalid)
    for error in result.diagnostics.errors:
        assert error.code == "IR011"
        assert error.path == (*prefix, "nodes", "subject", *suffix)
        assert error.offset == len(source)
        assert error.node == "subject"
        assert error.location is not None
        assert error.location.file == "node.py"


@pytest.mark.parametrize(("kind", "suffix"), PATH_SITES)
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("invalid", [False, True])
def test_expression_syntax_covers_path_fields(
    kind: str, suffix: FieldPath, nested: bool, invalid: bool
) -> None:
    source = "inputs.data[-1]" if invalid else "inputs.data[0]"
    graph = field_document(kind, suffix, source, nested=nested)
    prefix = ("graph", "subgraphs", "unused") if nested else ("graph",)

    result = validate_expression_syntax(graph)

    assert result.syntax_valid is not invalid
    assert result.paths_checked == 1
    assert result.expressions_checked == int(kind == "branch")
    assert len(result.diagnostics.errors) == int(invalid)
    for error in result.diagnostics.errors:
        assert error.code == "IR011"
        assert error.path == (*prefix, "nodes", "subject", *suffix)
        assert error.offset == source.index("-")


@pytest.mark.parametrize(
    ("path", "is_path", "owner"),
    [
        (("runtime", "infrastructure_retry", "predicate"), False, None),
        (("graph", "subgraphs", "unused", "retry", "predicate"), False, "body.py"),
        (("graph", "triggers", 1, "poll", "until"), False, "graph.py"),
        (("graph", "target", "key", "path"), True, "graph.py"),
    ],
)
def test_document_body_trigger_and_target_fields_keep_their_owner(
    path: FieldPath, is_path: bool, owner: str | None
) -> None:
    graph = root_field_document(path, "value.")

    result = validate_expression_syntax(graph)

    assert not result.syntax_valid
    assert result.nodes_checked == result.triggers_checked == 2
    assert result.definitions_checked == result.paths_checked == 1
    assert result.expressions_checked == (1 if is_path or "triggers" in path else 2)
    assert len(result.diagnostics.errors) == 1
    error = result.diagnostics.errors[0]
    assert error.path == path
    assert error.offset == len("value.")
    assert error.node is None
    assert (None if error.location is None else error.location.file) == owner


@pytest.mark.parametrize("kind", ["timer", "human_input"])
def test_expression_syntax_preserves_scoped_boundary(kind: str) -> None:
    value = node(kind, retry={"max_attempts": 1, "predicate": "unknown + 1"})
    value["inputs"] = {"data": {"type": "StructuredOutput", "path": "wrong_root.value"}}
    graph = document_for(nodes={"subject": value})
    before = export_ir(graph)

    result = validate_expression_syntax(graph)

    assert result.syntax_valid
    assert result.expressions_checked == result.paths_checked == 1
    assert export_ir(graph) == before


def test_disabled_infrastructure_retry_and_each_approval_binding_are_checked() -> None:
    value = document()
    set_field(
        value,
        ("runtime", "infrastructure_retry"),
        {"max_attempts": 1, "predicate": "true &&"},
    )
    approval = node("human_input")
    set_field(approval, ("config", "binds", "gate_digests"), ["inputs.a.", "inputs.b."])
    set_field(value, ("graph", "nodes"), {"approval": approval})
    graph = GraphDocument.model_validate(value)

    result = validate_expression_syntax(graph)

    assert result.expressions_checked == 1
    assert result.paths_checked == 2
    assert len(result.diagnostics.errors) == 3
    assert {error.path for error in result.diagnostics.errors} == {
        ("runtime", "infrastructure_retry", "predicate"),
        ("graph", "nodes", "approval", "config", "binds", "gate_digests", 0),
        ("graph", "nodes", "approval", "config", "binds", "gate_digests", 1),
    }


@pytest.mark.parametrize(("kind", "suffix"), PATH_SITES)
def test_authored_empty_paths_are_invalid(kind: str, suffix: FieldPath) -> None:
    graph = field_document(kind, suffix, "")

    result = validate_expression_syntax(graph)

    assert not result.syntax_valid
    assert result.paths_checked == 1
    assert result.diagnostics.errors[0].offset == 0


@pytest.mark.parametrize(
    ("source", "code", "offset"),
    [
        ("value[0x1]", "IR011", 6),
        ("value[1u]", "IR011", 6),
        ("value[other]", "IR011", 6),
        ("value + other", "IR011", 6),
        ("value.call()", "IR011", 10),
        ("if.value", "IR011", 0),
        ("root" + ".field" * 32 + ".overflow", "IR027", 197),
        ("root" + "[0]" * 33, "IR027", 102),
        ("root['😀']" + ".field" * 31 + ".overflow", "IR027", 196),
    ],
)
def test_path_syntax_reasons_and_exact_offsets(
    source: str, code: str, offset: int
) -> None:
    graph = field_document("timer", ("inputs", "data", "path"), source)

    result = validate_expression_syntax(graph)

    assert not result.syntax_valid
    assert result.diagnostics.errors[0].code == code
    assert result.diagnostics.errors[0].offset == offset


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("'😀' &&", "IR011"),
        ("unknown_function()", "IR011"),
        ("value.?field", "IR011"),
        ("items.all(a, items.all(b, items.all(c, true)))", "IR027"),
        (" && ".join("items.all(x, true)" for _ in range(5)), "IR027"),
        ("!" * 33 + "true", "IR027"),
        ("value.matches('" + "(" * 33 + "a" + ")" * 33 + "')", "IR027"),
        ("value.matches(pattern)", "IR011"),
    ],
)
def test_expression_syntax_preserves_existing_parser_limits(
    source: str, code: str
) -> None:
    graph = predicates(1, source)

    result = validate_expression_syntax(graph)

    assert not result.syntax_valid
    assert result.diagnostics.errors[0].code == code
    assert result.diagnostics.errors[0].offset is not None
    if source == "'😀' &&":
        assert result.diagnostics.errors[0].offset == len(source)


def test_expression_length_remains_a_structural_boundary() -> None:
    value = document()
    set_field(
        value, ("graph", "nodes", "timer", "retry", "predicate"), " " * 4093 + "true"
    )

    with pytest.raises(ValidationError) as caught:
        GraphDocument.model_validate(value)

    assert any(
        error["type"] == "string_too_long"
        for error in caught.value.errors(include_input=False)
    )


def test_shared_body_is_parsed_once_and_empty_fields_stay_absent() -> None:
    graph = shared_body(1000)
    before = export_ir(graph)

    result = validate_expression_syntax(graph)

    assert result.syntax_valid
    assert result.nodes_checked == 1001
    assert result.definitions_checked == result.expressions_checked == 1
    assert result.triggers_checked == result.paths_checked == 0
    assert export_ir(graph) == before


@pytest.mark.parametrize(
    "label",
    ["safe.py", "/private/source", "https://example.invalid/source", "x" * 1025],
)
def test_expression_diagnostics_never_fall_back_to_ancestor_source(label: str) -> None:
    value = node(retry={"predicate": "true &&"}, source={"file": label, "line": 9})
    graph = document_for(
        nodes={"subject": value}, source={"file": "ancestor.py", "line": 1}
    )

    result = validate_expression_syntax(graph)

    error = result.diagnostics.errors[0]
    assert (None if error.location is None else error.location.file) == (
        label if label == "safe.py" else None
    )


def test_opaque_strings_and_unwritten_defaults_are_not_parsed() -> None:
    value = node("agent")
    set_field(value, ("config", "prompt"), "{{ invalid.")
    set_field(value, ("config", "hooks"), {"predicate": "true &&"})
    set_field(value, ("config", "output_schema"), {"description": "true &&"})
    value["x-expression"] = "true &&"
    command = node("command")
    set_field(command, ("config", "cmd"), ["invalid."])
    graph = document_for(
        nodes={"agent": value, "command": command, "approval": node("human_input")}
    )
    before = export_ir(graph)

    result = validate_expression_syntax(graph)

    assert result.syntax_valid
    assert result.expressions_checked == result.paths_checked == 0
    assert export_ir(graph) == before


@pytest.mark.parametrize("byte_pressure", [False, True])
def test_expression_errors_preserve_count_and_byte_limits(byte_pressure: bool) -> None:
    nodes = {
        ("n" * 60 + str(index) if byte_pressure else f"n{index}"): node(
            retry={"predicate": "true &&"},
            source={"file": "s" * 1000, "line": 1}
            if byte_pressure
            else {"file": "safe.py", "line": 1},
        )
        for index in range(3000)
    }
    graph = document_for(nodes=nodes)

    result = validate_expression_syntax(graph)

    count = len(result.diagnostics.errors)
    assert count + result.diagnostics.errors_omitted == 3000
    assert 0 < count < 1000 if byte_pressure else count == 1000
    payload = {
        "errors": [
            error.model_dump(mode="json") for error in result.diagnostics.errors
        ],
        "warnings": [],
    }
    assert (
        len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        <= 1_048_576
    )
