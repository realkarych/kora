import json

import pytest

from kora.ir import (
    export_ir,
    validate_expression_syntax,
    validate_sensor_trigger_expressions,
)
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.trigger_expression_cases import expensive, sensor, triggers


@pytest.mark.parametrize(
    "text",
    [
        "true",
        "false",
        "observation",
        "observation.body.ready",
        "observation['body'].ready == true",
        "observation.missing == null",
        "observation.items.exists(item, item.ready)",
        "has(observation.ready) && observation.ready",
    ],
)
def test_sensor_trigger_checks_existing_dynamic_observation_context(text: str) -> None:
    document = triggers(1, text)
    before = export_ir(document)

    result = validate_sensor_trigger_expressions(document)

    assert result.expressions_valid
    assert (
        result.triggers_checked
        == result.sensor_triggers_checked
        == result.expressions_checked
        == 1
    )
    assert result.expressions_unresolved == 0
    assert result.syntax_steps > 0
    assert result.checker_steps > 0
    assert export_ir(document) == before


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("inputs.x == true", "identifier is not declared in this environment"),
        ("params.x == true", "identifier is not declared in this environment"),
        ("response.x == true", "identifier is not declared in this environment"),
        ("outputs.x == true", "identifier is not declared in this environment"),
        ("payload.x == true", "identifier is not declared in this environment"),
        ("unknown", "identifier is not declared in this environment"),
        ("1", "expression result does not have the required type"),
        ("'ready'", "expression result does not have the required type"),
        ("[true]", "expression result does not have the required type"),
        (
            "size(observation) == 0",
            "size() of a string or dyn value counts bytes, not characters",
        ),
        ("1 == 1.0", "no operator or function accepts these operand types"),
        (
            "observation.items.exists(observation, true)",
            "comprehension variable shadows an environment variable",
        ),
    ],
)
def test_sensor_trigger_reports_existing_checker_failures(
    text: str, message: str
) -> None:
    document = triggers(1, text)

    result = validate_sensor_trigger_expressions(document)

    assert not result.expressions_valid
    assert result.expressions_checked == 1
    assert result.expressions_unresolved == 0
    (error,) = result.diagnostics.errors
    assert error.code == "IR011"
    assert error.message == message
    assert error.path == ("graph", "triggers", 0, "poll", "until")
    assert error.node is None
    assert error.offset is not None
    assert error.location is not None
    assert (error.location.file, error.location.line) == ("graph.py", 3)


def test_sensor_trigger_preserves_existing_logical_cost_refusal() -> None:
    document = triggers(1, expensive())

    result = validate_sensor_trigger_expressions(document)
    syntax = validate_expression_syntax(document)

    assert syntax.syntax_valid
    assert not result.expressions_valid
    assert result.expressions_checked == 1
    (error,) = result.diagnostics.errors
    assert error.code == "IR027"
    assert error.path == ("graph", "triggers", 0, "poll", "until")
    assert error.offset is not None


@pytest.mark.parametrize(
    "text", ["true &&", "unknown_function()", "(" * 33 + "true" + ")" * 33]
)
def test_sensor_trigger_syntax_failures_remain_unresolved_without_duplicates(
    text: str,
) -> None:
    document = triggers(1, text)

    result = validate_sensor_trigger_expressions(document)
    syntax = validate_expression_syntax(document)

    assert not result.expressions_valid
    assert result.diagnostics.valid
    assert result.expressions_checked == result.checker_steps == 0
    assert result.expressions_unresolved == 1
    assert len(syntax.diagnostics.errors) == 1


def test_sensor_trigger_counts_all_kinds_and_preserves_independent_indexes() -> None:
    document = document_for(
        triggers=[
            {"kind": "manual"},
            sensor("unknown"),
            {"kind": "cron", "expr": "not CEL"},
            sensor("true &&"),
            {"kind": "graph", "graph": "other", "on": "run_failed"},
            sensor("false"),
        ],
        params={"type": "object", "properties": {"x": {"type": "boolean"}}},
        nodes={
            "wait": node(
                "wait_event",
                config={
                    "poll": {"sensor": "http", "until": "params.x"},
                    "timeout": "0s",
                    "on_expiry": "skip",
                },
            )
        },
        bodies={"unused": {"nodes": {"branch": node("branch")}}},
    )
    before = export_ir(document)

    result = validate_sensor_trigger_expressions(document)

    assert result.triggers_checked == 6
    assert result.sensor_triggers_checked == 3
    assert result.expressions_checked == 2
    assert result.expressions_unresolved == 1
    assert len(result.diagnostics.errors) == 1
    assert result.diagnostics.errors[0].path == (
        "graph",
        "triggers",
        1,
        "poll",
        "until",
    )
    assert export_ir(document) == before


def test_sensor_trigger_ignores_opaque_configuration_without_provider_authority() -> (
    None
):
    trigger = sensor("observation.ready")
    trigger["poll"] = {
        "sensor": "unregistered",
        "config": {"until": "unknown &&", "schema": {"type": "boolean"}},
        "until": "observation.ready",
    }
    trigger["params"] = {"until": "unknown &&"}
    document = document_for(triggers=[trigger])

    result = validate_sensor_trigger_expressions(document)

    assert result.expressions_valid
    assert result.expressions_checked == 1


@pytest.mark.parametrize("authored", [False, True])
def test_sensor_trigger_preserves_absent_or_empty_trigger_lists(authored: bool) -> None:
    document = document_for(triggers=[]) if authored else document_for()
    before = export_ir(document)

    result = validate_sensor_trigger_expressions(document)

    assert result.expressions_valid
    assert (
        result.triggers_checked
        == result.sensor_triggers_checked
        == result.expressions_checked
        == 0
    )
    assert (
        result.syntax_steps
        == result.checker_steps
        == result.expressions_unresolved
        == 0
    )
    assert export_ir(document) == before


@pytest.mark.parametrize(
    "source", [None, "/private/graph.py", "https://host/graph.py", "x" * 1025]
)
def test_sensor_trigger_never_substitutes_unsafe_graph_source(
    source: str | None,
) -> None:
    fields: dict[str, object] = {}
    if source is not None:
        fields["source"] = {"file": source, "line": 3}
    document = document_for(
        None,
        {"timer": node(source={"file": "node.py", "line": 9})},
        None,
        None,
        triggers=[sensor("unknown")],
        **fields,
    )

    result = validate_sensor_trigger_expressions(document)

    (error,) = result.diagnostics.errors
    assert error.location is None
    assert error.node is None
    assert error.offset == 0


def test_sensor_trigger_retains_non_bmp_code_point_offset() -> None:
    text = "'😀' == '😀' && unknown"
    document = triggers(1, text)

    result = validate_sensor_trigger_expressions(document)

    (error,) = result.diagnostics.errors
    assert error.offset == text.index("unknown")
    assert error.path == ("graph", "triggers", 0, "poll", "until")
    assert "😀" not in error.message


@pytest.mark.parametrize("byte_pressure", [False, True])
def test_sensor_trigger_diagnostics_are_bounded_and_content_free(
    byte_pressure: bool,
) -> None:
    marker = "private_sensor_predicate_content"
    document = document_for(
        triggers=[sensor(marker)] * 3000,
        source={"file": "s" * (1000 if byte_pressure else 1), "line": 3},
    )

    result = validate_sensor_trigger_expressions(document)
    payload = json.dumps(
        {
            "errors": [e.model_dump(mode="json") for e in result.diagnostics.errors],
            "warnings": [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()

    assert not result.expressions_valid
    assert result.expressions_checked == result.sensor_triggers_checked == 3000
    assert len(result.diagnostics.errors) + result.diagnostics.errors_omitted == 3000
    assert (
        len(result.diagnostics.errors) < 1000
        if byte_pressure
        else len(result.diagnostics.errors) == 1000
    )
    assert len(payload) <= 1_048_576
    assert marker.encode() not in payload
