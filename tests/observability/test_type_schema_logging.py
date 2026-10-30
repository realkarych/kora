import pytest

from kora.ir import validate_type_schemas
from kora.observability import Level, LoggingSettings
from tests.ir.type_schema_cases import document_for
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("invalid", [False, True])
def test_type_schema_logging_is_content_free(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], invalid: bool
) -> None:
    marker = "private_schema_content"
    document = document_for(
        {
            "description": marker,
            "properties": {
                marker * 100: {
                    "$ref": "https://example.invalid/" + marker if invalid else "#"
                }
            },
        }
    )
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    result = validate_type_schemas(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.type_schemas.completed"
    ]
    assert result.schemas_valid is not invalid
    assert len(records) == 1
    assert records[0]["level"] == "INFO"
    assert records[0]["component"] == "ir.validation"
    assert records[0]["schemas_valid"] is not invalid
    assert records[0]["errors"] == int(invalid)
    assert records[0]["errors_omitted"] == 0
    assert records[0]["contracts"] == 1
    assert records[0]["schemas"] == 2
    assert records[0]["references"] == 1
    assert marker not in output
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_type_schema_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = document_for({"type": "invalid"})
    expected = validate_type_schemas(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_type_schemas(document)
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
