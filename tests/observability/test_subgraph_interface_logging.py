import pytest

from kora.ir import validate_subgraph_interfaces
from kora.observability import Level, LoggingSettings
from tests.ir.inline_schema_cases import document_for
from tests.ir.subgraph_interface_cases import (
    interface_body,
    interface_wrapper,
    interfaces,
)
from tests.ir.type_declaration_cases import custom
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("invalid", [False, True])
def test_subgraph_interface_logging_is_content_free(
    configure: Configure,
    pipe: Pipe,
    capsys: pytest.CaptureFixture[str],
    invalid: bool,
) -> None:
    marker = "private_interface_content"
    first = marker + "/A"
    second = marker + "/B"
    saved = custom()
    saved["schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "description": marker,
        "$ref": "https://example.invalid/" + marker,
    }
    document = document_for(
        contracts={first: saved, second: custom()},
        nodes={
            marker: interface_wrapper(
                second if invalid else first,
                first,
                source={"file": marker + ".py", "line": 1},
            )
        },
        bodies={"body": interface_body(first, first)},
    )
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    result = validate_subgraph_interfaces(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.subgraph_interfaces.completed"
    ]
    assert result.interfaces_valid is not invalid
    assert len(records) == 1
    record = records[0]
    assert record["level"] == "INFO"
    assert record["component"] == "ir.validation"
    assert record["interfaces_valid"] is not invalid
    assert record["errors"] == int(invalid)
    assert record["errors_omitted"] == 0
    assert (
        record["nodes"]
        == record["contracts"]
        == record["types_indexed"]
        == record["compared"]
        == 2
    )
    assert (
        record["definitions"]
        == record["wrappers"]
        == record["inputs"]
        == record["outputs"]
        == 1
    )
    assert (
        record["wrappers_unresolved"]
        == record["unresolved"]
        == record["names_mismatched"]
        == 0
    )
    assert marker not in output
    assert all(marker not in error.message for error in result.diagnostics.errors)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_subgraph_interface_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = interfaces(1, invalid=True, depth=2)
    expected = validate_subgraph_interfaces(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        assert validate_subgraph_interfaces(document) == expected
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
