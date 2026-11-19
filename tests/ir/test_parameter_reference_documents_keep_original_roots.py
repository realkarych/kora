import json

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
