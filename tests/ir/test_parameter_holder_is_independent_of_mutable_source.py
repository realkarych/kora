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
