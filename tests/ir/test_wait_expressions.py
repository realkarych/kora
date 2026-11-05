import gc
import json
from weakref import ref

import pytest

from kora.cel import CheckedExpression, Environment, SchemaTypes
from kora.cel.tree import Node
from kora.ir import export_ir, validate_expression_syntax, validate_wait_expressions
from kora.ir.approval_inputs import DirectInputContexts
from kora.ir.expression_contexts import InputParameterContexts
from kora.ir.json import JsonObject
from tests.ir.approval_input_cases import unavailable_inputs
from tests.ir.branch_expression_cases import SCHEMA
from tests.ir.inline_schema_cases import document_for
from tests.ir.trigger_expression_cases import expensive
from tests.ir.wait_expression_cases import wait, wait_document


@pytest.mark.parametrize("poll", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(
    ("text", "valid"),
    [
        ("external.ready && inputs.data.ok && params.ok", True),
        ("external.body.ready == true", True),
        ("external['key'].ready", True),
        ("external.ready", True),
        ('params.mode == "run"', True),
        ("true", True),
        ("1", False),
        ('"literal"', False),
        ("null", False),
        ("response.ok", False),
        ("outputs.ok", False),
        ("inputs.data.missing", False),
        ('params.mode == "missing"', False),
        ("inputs == null", False),
        ("[1].exists(params, true)", False),
        ("[1].exists(inputs, true)", False),
        ("[1].exists(external, true)", False),
        ("size(external) > 0", False),
    ],
)
def test_wait_fields_use_declared_context_and_boolean_results(
    poll: bool, nested: bool, text: str, valid: bool
) -> None:
    document = wait_document(text, poll=poll, nested=nested)
    original = export_ir(document)

    result = validate_wait_expressions(document)

    assert result.wait_expressions_complete
    assert result.wait_expressions_valid is valid
    assert result.expressions_seen == result.expressions_checked == 1
    assert result.waits_checked == 1
    assert result.definitions_checked == int(nested)
    assert bool(result.diagnostics.errors) is not valid
    prefix = ("graph", "subgraphs", "unused") if nested else ("graph",)
    field = ("poll", "until") if poll else ("event", "match")
    assert all(
        error.code == "IR011"
        and error.path == (*prefix, "nodes", "wait", "config", *field)
        and error.location is not None
        and (error.location.file, error.location.line) == ("wait.py", 7)
        and error.node == "wait"
        for error in result.diagnostics.errors
    )
    assert export_ir(document) == original


@pytest.mark.parametrize("poll", [False, True])
def test_wait_isolates_event_payload_and_poll_observation(poll: bool) -> None:
    forbidden = "payload" if poll else "observation"
    document = wait_document(forbidden + ".ready", poll=poll)

    result = validate_wait_expressions(document)

    assert result.wait_expressions_complete
    assert not result.wait_expressions_valid
    assert result.diagnostics.errors[0].code == "IR011"
    assert result.diagnostics.errors[0].offset == 0


@pytest.mark.parametrize(
    ("text", "valid"),
    [("inputs.data.ok", False), ("has(inputs.data) && inputs.data.ok", True)],
)
def test_wait_preserves_optional_input_guard_rules(text: str, valid: bool) -> None:
    document = wait_document(text, optional=True)

    result = validate_wait_expressions(document)

    assert result.wait_expressions_complete
    assert result.wait_expressions_valid is valid


@pytest.mark.parametrize("schema", [None, {}])
def test_wait_parameter_omission_and_explicit_dynamic_schema_remain_distinct(
    schema: JsonObject | None,
) -> None:
    document = document_for(params=schema, nodes={"wait": wait("params.anything")})

    result = validate_wait_expressions(document)

    assert result.wait_expressions_complete
    assert result.wait_expressions_valid is (schema == {})


def test_wait_match_omission_stays_absent_without_synthetic_expression() -> None:
    document = document_for(
        params={"$ref": "https://example.invalid/private-schema"},
        nodes={"wait": wait(None, inputs=unavailable_inputs())},
    )
    original = export_ir(document)

    result = validate_wait_expressions(document)

    assert result.wait_expressions_valid
    assert result.waits_checked == 1
    assert result.expressions_seen == result.expressions_checked == 0
    assert result.schema_steps == result.checker_steps == 0
    assert export_ir(document) == original


def test_wait_preserves_incomplete_prerequisites_and_independent_errors() -> None:
    document = document_for(
        params={"$ref": "https://example.invalid/private-schema"},
        nodes={
            "literal": wait("payload.ready", inputs=unavailable_inputs()),
            "schema": wait("params.ok"),
            "context": wait("inputs.unknown", inputs=unavailable_inputs()),
            "syntax": wait("true &&", poll=True),
            "invalid": wait("unknown", poll=True),
        },
    )

    result = validate_wait_expressions(document)
    syntax = validate_expression_syntax(document)

    assert not result.wait_expressions_complete
    assert not result.wait_expressions_valid
    assert result.expressions_seen == 5
    assert result.expressions_checked == 2
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
        "poll",
        "until",
    )


def test_wait_preserves_non_bmp_codepoint_offsets() -> None:
    text = '"😀" == "😀" && missing'
    document = wait_document(text)

    result = validate_wait_expressions(document)

    assert result.wait_expressions_complete
    assert not result.wait_expressions_valid
    assert result.diagnostics.errors[0].offset == text.index("missing")


def test_wait_preserves_existing_logical_cost_refusal() -> None:
    document = document_for(nodes={"wait": wait(expensive(), poll=True)})

    result = validate_wait_expressions(document)

    assert result.wait_expressions_complete
    assert not result.wait_expressions_valid
    assert result.diagnostics.errors[0].code == "IR027"


def test_wait_diagnostics_are_shared_bounded_and_content_free() -> None:
    marker = "private_wait_expression_content"
    document = document_for(nodes={f"wait{i}": wait(marker) for i in range(1002)})

    result = validate_wait_expressions(document)
    encoded = json.dumps(
        [error.model_dump(mode="json") for error in result.diagnostics.errors]
    )

    assert len(result.diagnostics.errors) == 1000
    assert result.diagnostics.errors_omitted == 2
    assert not result.wait_expressions_complete
    assert not result.wait_expressions_valid
    assert marker not in encoded


def test_wait_shared_context_reuses_parameter_mapping_across_modes() -> None:
    one = wait_document("params.ok")
    many = document_for(
        params=SCHEMA,
        nodes={f"wait{i}": wait("params.ok", poll=bool(i % 2)) for i in range(100)},
    )

    single = validate_wait_expressions(one)
    repeated = validate_wait_expressions(many)

    assert single.wait_expressions_valid
    assert repeated.wait_expressions_valid
    assert repeated.schema_steps == single.schema_steps
    assert repeated.checker_steps == single.checker_steps * 100
    assert repeated.expressions_seen == repeated.expressions_checked == 100


@pytest.mark.parametrize("source", ["params.ok", "unknown", "true &&"])
def test_wait_result_releases_context_and_document(source: str) -> None:
    document = wait_document(source)
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

    result = validate_wait_expressions(document)
    del document
    gc.collect()
    remaining = sum(isinstance(value, owned) for value in gc.get_objects())

    assert document_ref() is None
    assert remaining == baseline
    assert result.expressions_seen == 1
