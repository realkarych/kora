import pytest

from kora.ir.standard_parameter_preparation import (
    PARAMETER_INTERPRETATION,
    StandardParameterInput,
    prepare_standard_parameters,
)
from kora.ir.standard_schema_policy import SchemaDomain, SchemaRefused
from tests.ir.standard_schema_cases import binding


def candidate(schema: object, documents: object = None) -> StandardParameterInput:
    return StandardParameterInput(
        {"interpretation": PARAMETER_INTERPRETATION, "schema": schema},
        [] if documents is None else documents,
        binding(SchemaDomain.PARAMETERS),
    )


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
