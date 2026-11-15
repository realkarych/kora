import json
import sys
from typing import cast

import pytest

from kora.ir.standard_parameter_preparation import (
    PARAMETER_INTERPRETATION,
    StandardParameterInput,
    prepare_standard_parameters,
    validate_standard_parameters,
)
from kora.ir.standard_schema_policy import SchemaDomain, SchemaRefused
from kora.observability import Level
from kora.observability.context import bound_scope
from tests.ir.standard_schema_cases import binding
from tests.ir.test_standard_schema_source_lifetime import diagnostics_pipe


def candidate(schema: object, documents: object = None) -> StandardParameterInput:
    return StandardParameterInput(
        {"interpretation": PARAMETER_INTERPRETATION, "schema": schema},
        [] if documents is None else documents,
        binding(SchemaDomain.PARAMETERS),
    )


@pytest.mark.parametrize(
    ("schema", "value", "outcome"),
    [
        ({"properties": {}, "additionalProperties": False}, {}, "valid"),
        ({"properties": {}, "additionalProperties": False}, {"x": 1}, "invalid"),
        ({}, {"x": 1}, "valid"),
        ({"properties": {"x": {"multipleOf": 0.1}}}, {"x": 0.3}, "valid"),
        ({"properties": {"x": {"const": 1}}}, {"x": True}, "invalid"),
        ({"properties": {"x": {"const": 0}}}, {"x": -0.0}, "valid"),
        ({}, {"x": -(2**63)}, "valid"),
        ({}, {"x": 2**63 - 1}, "valid"),
        ({}, {"x": 2**63}, "refused"),
        ({}, {"x": -(2**63) - 1}, "refused"),
        ({"properties": {"x": {"default": 3}}}, {}, "valid"),
    ],
)
def test_parameter_composition_preserves_original_value(
    schema: object, value: object, outcome: str
) -> None:
    prepared = prepare_standard_parameters(candidate(schema))
    original = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    references = sys.getrefcount(value)

    result = validate_standard_parameters(value, prepared)

    assert result.outcome == outcome
    assert json.dumps(value, ensure_ascii=False, separators=(",", ":")) == original
    assert sys.getrefcount(value) == references


def test_parameter_reference_documents_keep_original_roots() -> None:
    leaf = {"$ref": "#/$defs/leaf", "$defs": {"leaf": {"const": 0.3}}}
    value = candidate(
        {
            "properties": {
                "x": {"$ref": "urn:kora:artifact:example:First"},
                "y": {"$dynamicRef": "urn:kora:artifact:example:Second"},
            }
        },
        [
            {"name": "example/First", "schema": leaf},
            {"name": "example/Second", "schema": json.loads(json.dumps(leaf))},
        ],
    )

    prepared = prepare_standard_parameters(value)
    result = validate_standard_parameters({"x": 0.3, "y": 0.3}, prepared)

    assert result.outcome == "valid"
    assert prepared.accounting.schema_documents == 3
    saved = json.loads(prepared.source_bytes)
    assert saved["constraints"] == [0]
    assert saved["documents"][1]["schema"] == leaf
    assert saved["documents"][2]["schema"] == leaf


@pytest.mark.parametrize(
    "schema",
    [
        {"anyOf": [True, {"pattern": "private-pattern"}]},
        {"$ref": "#"},
        {"$ref": "https://private.invalid/schema"},
        {"$defs": {"unused": {"unevaluatedProperties": False}}},
    ],
)
def test_parameter_source_refuses_incomplete_support(schema: object) -> None:
    value = candidate(schema)

    with pytest.raises(SchemaRefused) as captured:
        prepare_standard_parameters(value)

    assert captured.value.reason == "unsupported_schema"
    assert str(captured.value) == "unsupported_schema"


@pytest.mark.parametrize("defect", ["output", "v1", "boolean", "order", "missing"])
def test_parameter_binding_cannot_inherit_another_interpretation(defect: str) -> None:
    profile = binding(SchemaDomain.PARAMETERS)
    limits = cast("dict[str, object]", profile["limits"])
    if defect == "output":
        profile = binding(SchemaDomain.OUTPUT)
    elif defect == "v1":
        profile["profile"] = "kora.graph-parameter-evaluation.v1"
    elif defect == "boolean":
        limits["max_value_depth"] = True
    elif defect == "order":
        profile["limits"] = dict(reversed(tuple(limits.items())))
    else:
        del limits["max_value_nodes"]
    value = StandardParameterInput(candidate({}).parameters, [], profile)

    with pytest.raises(SchemaRefused) as captured:
        prepare_standard_parameters(value)

    assert captured.value.reason == "unsupported_policy"


def test_parameter_binding_preserves_existing_member_order_tolerance() -> None:
    value = StandardParameterInput(
        {"schema": {}, "interpretation": PARAMETER_INTERPRETATION},
        [],
        binding(SchemaDomain.PARAMETERS),
    )

    prepared = prepare_standard_parameters(value)
    result = validate_standard_parameters({"x": [1]}, prepared)

    assert result.outcome == "valid"


@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
def test_parameter_composition_diagnostics_exclude_original_content(
    level: Level,
) -> None:
    marker = "private_parameter_composition_marker"
    value = candidate({"properties": {marker: {"const": marker}}})

    with diagnostics_pipe(level) as (diagnostics, pipe):
        pipe.reader.start()
        with bound_scope(request_id="parameter-composition-request"):
            prepared = prepare_standard_parameters(value)
            result = validate_standard_parameters({marker: "mismatch"}, prepared)
        drained = diagnostics.shutdown()
        pipe.close_write_end()
        pipe.reader.wait_closed()
        records = pipe.reader.records()
        output = pipe.reader.data

    assert drained
    assert result.outcome == "invalid"
    assert marker.encode() not in output
    assert marker not in repr(result)
    assert marker not in repr(prepared)
    events = [record["event"] for record in records if record["component"] == "ir"]
    assert events == (
        [
            "ir.standard_schema_source.completed",
            "ir.standard_schema_validation.completed",
        ]
        if level is Level.DEBUG
        else []
    )


def test_parameter_holder_is_independent_of_mutable_source() -> None:
    schema: dict[str, object] = {"properties": {"x": {"const": 1}}}
    value = candidate(schema)
    prepared = prepare_standard_parameters(value)
    original = prepared.source_bytes
    schema.clear()
    schema["not"] = {}

    result = validate_standard_parameters({"x": 1}, prepared)

    assert result.outcome == "valid"
    assert prepared.source_bytes == original
