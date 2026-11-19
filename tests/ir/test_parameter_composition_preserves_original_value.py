import json
import sys

import pytest

from kora.ir.standard_parameter_preparation import (
    PARAMETER_INTERPRETATION,
    StandardParameterInput,
    prepare_standard_parameters,
    validate_standard_parameters,
)
from kora.ir.standard_schema_policy import SchemaDomain
from tests.ir.standard_schema_cases import binding


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
