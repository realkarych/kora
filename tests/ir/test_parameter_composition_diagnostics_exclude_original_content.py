import pytest

from kora.ir.standard_parameter_preparation import (
    PARAMETER_INTERPRETATION,
    StandardParameterInput,
    prepare_standard_parameters,
    validate_standard_parameters,
)
from kora.ir.standard_schema_policy import SchemaDomain
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
