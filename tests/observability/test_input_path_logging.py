import pytest

from kora.ir import validate_input_paths
from kora.observability import Level, LoggingSettings
from tests.ir.inline_schema_cases import document_for
from tests.ir.input_path_cases import binding_node, bindings
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("invalid", [False, True])
def test_input_path_logging_is_content_free(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], invalid: bool
) -> None:
    marker = "private_path_content"
    value = binding_node(f"{marker}.data" if invalid else f"inputs.data.{marker}")
    value["source"] = {"file": marker + ".py", "line": 1}
    graph = document_for(nodes={"subject": value})
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    result = validate_input_paths(graph)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.input_paths.completed"
    ]
    assert result.input_paths_valid is not invalid
    assert len(records) == 1
    record = records[0]
    assert record["level"] == "INFO"
    assert record["component"] == "ir.validation"
    assert record["input_paths_valid"] is not invalid
    assert record["errors"] == int(invalid)
    assert (
        record["errors_omitted"] == record["definitions"] == record["unresolved"] == 0
    )
    assert record["nodes"] == record["bindings"] == record["paths"] == 1
    assert marker not in output
    assert all(marker not in error.message for error in result.diagnostics.errors)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_input_path_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    graph = bindings(1, "inputs.missing")
    expected = validate_input_paths(graph)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_input_paths(graph)
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
