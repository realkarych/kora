import gc
import json
from weakref import ref

import pytest

from kora.cel import CheckedExpression, Environment, SchemaTypes
from kora.cel.tree import Node
from kora.ir import export_ir, validate_branch_expressions, validate_expression_syntax
from kora.ir.approval_inputs import DirectInputContexts
from kora.ir.expression_contexts import InputParameterContexts
from kora.ir.json import JsonObject
from tests.ir.approval_input_cases import unavailable_inputs
from tests.ir.branch_expression_cases import SCHEMA, branch, branch_document
from tests.ir.inline_schema_cases import document_for
from tests.ir.trigger_expression_cases import expensive


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(
    ("when", "reason", "valid"),
    [
        ("inputs.data.ok && params.ok", "inputs.data.label", True),
        ('params.mode == "run"', "params.label", True),
        ("true", '"literal"', True),
        ("1", '"literal"', False),
        ('"literal"', '"literal"', False),
        ("true", "false", False),
        ("true", "1", False),
        ("true", "null", False),
        ("response.ok", '"literal"', False),
        ("payload.ok", '"literal"', False),
        ("observation.ok", '"literal"', False),
        ("outputs.ok", '"literal"', False),
        ("inputs.data.missing", '"literal"', False),
        ('params.mode == "missing"', '"literal"', False),
        ("inputs == null", '"literal"', False),
        ("[1].exists(params, true)", '"literal"', False),
        ("[1].exists(inputs, true)", '"literal"', False),
    ],
)
def test_branch_fields_use_declared_context_and_required_result_types(
    nested: bool, when: str, reason: str, valid: bool
) -> None:
    document = branch_document(when, reason, nested=nested)
    original = export_ir(document)

    result = validate_branch_expressions(document)

    assert result.branch_expressions_complete
    assert result.branch_expressions_valid is valid
    assert result.expressions_seen == result.expressions_checked == 2
    assert result.branches_checked == 1
    assert result.definitions_checked == int(nested)
    assert bool(result.diagnostics.errors) is not valid
    prefix = ("graph", "subgraphs", "unused") if nested else ("graph",)
    assert all(
        error.code == "IR011"
        and error.path is not None
        and error.path[: len(prefix) + 2] == (*prefix, "nodes", "branch")
        and error.location is not None
        and (error.location.file, error.location.line) == ("branch.py", 7)
        and error.node == "branch"
        for error in result.diagnostics.errors
    )
    assert export_ir(document) == original


@pytest.mark.parametrize(
    ("when", "valid"),
    [("inputs.data.ok", False), ("has(inputs.data) && inputs.data.ok", True)],
)
def test_branch_preserves_optional_input_guard_rules(when: str, valid: bool) -> None:
    document = branch_document(when, optional=True)

    result = validate_branch_expressions(document)

    assert result.branch_expressions_complete
    assert result.branch_expressions_valid is valid


@pytest.mark.parametrize("schema", [None, {}])
def test_branch_parameter_omission_and_explicit_dynamic_schema_remain_distinct(
    schema: JsonObject | None,
) -> None:
    document = document_for(params=schema, nodes={"branch": branch("params.anything")})

    result = validate_branch_expressions(document)

    assert result.branch_expressions_complete
    assert result.branch_expressions_valid is (schema == {})


def test_branch_checks_dormant_reasons_and_non_bmp_offsets() -> None:
    text = '"😀" == "😀" ? missing : "reason"'
    document = branch_document("false", text, "true")

    result = validate_branch_expressions(document)

    assert result.expressions_seen == result.expressions_checked == 3
    assert result.branch_expressions_complete
    assert not result.branch_expressions_valid
    assert [(error.path, error.offset) for error in result.diagnostics.errors] == [
        (
            ("graph", "nodes", "branch", "config", "cases", 0, "reason"),
            text.index("missing"),
        ),
        (("graph", "nodes", "branch", "config", "else_reason"), 0),
    ]


def test_branch_preserves_incomplete_prerequisites_and_independent_errors() -> None:
    document = document_for(
        params={"$ref": "https://example.invalid/private-schema"},
        nodes={
            "literal": branch("true", '"reason"', inputs=unavailable_inputs()),
            "schema": branch("params.ok"),
            "context": branch("inputs.unknown", inputs=unavailable_inputs()),
            "syntax": branch("true &&"),
            "invalid": branch("unknown"),
        },
    )

    result = validate_branch_expressions(document)
    syntax = validate_expression_syntax(document)

    assert not result.branch_expressions_complete
    assert not result.branch_expressions_valid
    assert result.expressions_seen == 6
    assert result.expressions_checked == 3
    assert (
        result.syntax_unresolved
        == result.context_unresolved
        == result.schema_unresolved
        == 1
    )
    assert len(result.diagnostics.errors) == len(syntax.diagnostics.errors) == 1
    assert result.diagnostics.errors[0].path == (
        "graph",
        "nodes",
        "invalid",
        "config",
        "cases",
        0,
        "when",
    )


def test_branch_preserves_existing_logical_cost_refusal() -> None:
    document = document_for(
        params={},
        nodes={"branch": branch(expensive().replace("observation", "params"))},
    )

    result = validate_branch_expressions(document)

    assert result.branch_expressions_complete
    assert not result.branch_expressions_valid
    assert result.diagnostics.errors[0].code == "IR027"


def test_branch_diagnostics_are_shared_bounded_and_content_free() -> None:
    marker = "private_branch_expression_content"
    document = document_for(nodes={f"branch{i}": branch(marker) for i in range(1002)})

    result = validate_branch_expressions(document)
    encoded = json.dumps(
        [error.model_dump(mode="json") for error in result.diagnostics.errors]
    )

    assert len(result.diagnostics.errors) == 1000
    assert result.diagnostics.errors_omitted == 2
    assert not result.branch_expressions_complete
    assert not result.branch_expressions_valid
    assert marker not in encoded


def test_branch_shared_context_reuses_parameter_mapping_across_fields() -> None:
    one = branch_document("params.ok", "params.label", "params.label")
    many = document_for(
        params=SCHEMA,
        nodes={
            f"branch{i}": branch("params.ok", "params.label", "params.label")
            for i in range(100)
        },
    )

    single = validate_branch_expressions(one)
    repeated = validate_branch_expressions(many)

    assert single.branch_expressions_valid
    assert repeated.branch_expressions_valid
    assert repeated.schema_steps == single.schema_steps
    assert repeated.checker_steps == single.checker_steps * 100
    assert repeated.expressions_seen == repeated.expressions_checked == 300


@pytest.mark.parametrize("source", ["params.ok", "unknown", "true &&"])
def test_branch_result_releases_context_and_document(source: str) -> None:
    document = branch_document(source)
    document_ref = ref(document)
    owned = (
        InputParameterContexts
        | DirectInputContexts
        | SchemaTypes
        | CheckedExpression
        | Environment
        | Node
    )
    gc.collect()
    baseline = sum(isinstance(value, owned) for value in gc.get_objects())

    result = validate_branch_expressions(document)
    del document
    gc.collect()
    remaining = sum(isinstance(value, owned) for value in gc.get_objects())

    assert document_ref() is None
    assert remaining == baseline
    assert result.expressions_seen == 1
