import pytest

from kora.ir import validate_export_types
from kora.observability import Level, LoggingSettings
from tests.ir.edge_type_cases import nested_type
from tests.ir.export_type_cases import export_body, exports
from tests.ir.inline_schema_cases import document_for
from tests.ir.type_declaration_cases import custom
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("invalid", [False, True])
@pytest.mark.parametrize("depth", [0, 2])
def test_export_type_logging_is_content_free(
    configure: Configure,
    pipe: Pipe,
    capsys: pytest.CaptureFixture[str],
    invalid: bool,
    depth: int,
) -> None:
    marker = "private_type_content"
    first = marker + "/A"
    second = marker + "/B"
    contract = custom()
    contract["schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "description": marker,
        "$ref": "https://example.invalid/" + marker,
    }
    body = export_body(
        nested_type(first, depth), nested_type(second if invalid else first, depth)
    )
    body["source"] = {"file": marker + ".py", "line": 1}
    document = document_for(
        contracts={first: contract, second: custom()}, bodies={marker: body}
    )
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    result = validate_export_types(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.export_types.completed"
    ]
    assert result.export_types_valid is not invalid
    assert len(records) == 1
    assert records[0]["level"] == "INFO"
    assert records[0]["component"] == "ir.validation"
    assert records[0]["export_types_valid"] is not invalid
    assert records[0]["errors"] == int(invalid)
    assert records[0]["errors_omitted"] == 0
    assert records[0]["contracts"] == records[0]["types_indexed"] == 2
    assert (
        records[0]["definitions"]
        == records[0]["exports"]
        == records[0]["compared"]
        == 1
    )
    assert records[0]["unresolved"] == 0
    assert marker not in output
    assert all(marker not in error.message for error in result.diagnostics.errors)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
@pytest.mark.parametrize("depth", [0, 2])
def test_export_type_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool, depth: int
) -> None:
    document = exports(1, invalid=True, list_depth=depth)
    expected = validate_export_types(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_export_types(document)
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
