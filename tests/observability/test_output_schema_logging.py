import pytest

from kora.ir import validate_output_schemas
from kora.ir.json import JsonObject
from kora.observability import Level, LoggingSettings
from tests.ir.inline_schema_cases import document_for
from tests.ir.output_schema_cases import human
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("invalid", [False, True])
def test_output_schema_logging_is_content_free(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], invalid: bool
) -> None:
    marker = "private_schema_content"
    left: JsonObject = {
        "description": marker,
        "properties": {marker * 100: {"$ref": "#"}},
    }
    right: JsonObject = {
        "description": marker,
        "properties": {
            marker * 100: {
                "$ref": "https://example.invalid/" + marker if invalid else "#"
            }
        },
    }
    document = document_for(
        nodes={"gate": human(left, right, source={"file": marker + ".py", "line": 1})}
    )
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    result = validate_output_schemas(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.output_schemas.completed"
    ]
    assert result.output_schemas_valid is not invalid
    assert len(records) == 1
    assert records[0]["level"] == "INFO"
    assert records[0]["component"] == "ir.validation"
    assert records[0]["output_schemas_valid"] is not invalid
    assert records[0]["errors"] == int(invalid)
    assert records[0]["errors_omitted"] == 0
    assert records[0]["nodes"] == 1
    assert records[0]["definitions"] == 0
    assert records[0]["outputs"] == 1
    assert records[0]["ancestry_steps"] == 0
    assert records[0]["comparisons"] == 1
    assert records[0]["values"] == 5
    assert marker not in output
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_output_schema_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = document_for(nodes={"gate": human({}, {"type": "string"})})
    expected = validate_output_schemas(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_output_schemas(document)
        assert actual == expected
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
