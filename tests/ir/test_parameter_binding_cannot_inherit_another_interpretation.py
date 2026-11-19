from typing import cast

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
