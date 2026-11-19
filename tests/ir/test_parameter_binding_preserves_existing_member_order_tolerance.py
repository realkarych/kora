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


def test_parameter_binding_preserves_existing_member_order_tolerance() -> None:
    value = StandardParameterInput(
        {"schema": {}, "interpretation": PARAMETER_INTERPRETATION},
        [],
        binding(SchemaDomain.PARAMETERS),
    )

    prepared = prepare_standard_parameters(value)
    result = validate_standard_parameters({"x": [1]}, prepared)

    assert result.outcome == "valid"
