import pytest

from kora.ir import validate_sensor_trigger_expressions
from kora.observability import Level, LoggingSettings
from tests.ir.inline_schema_cases import document_for
from tests.ir.trigger_expression_cases import sensor, triggers
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("ending", ["valid", "invalid", "unresolved"])
def test_sensor_trigger_logging_is_content_free(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], ending: str
) -> None:
    marker = "private_sensor_expression_content"
    text = {
        "valid": "observation." + marker,
        "invalid": marker,
        "unresolved": marker + " &&",
    }[ending]
    trigger = sensor(text)
    trigger["params"] = {marker: marker}
    document = document_for(
        triggers=[trigger], source={"file": marker + ".py", "line": 3}
    )
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    result = validate_sensor_trigger_expressions(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.sensor_trigger_expressions.completed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record["level"] == "INFO"
    assert record["component"] == "ir.validation"
    assert record["expressions_valid"] is (ending == "valid")
    assert record["errors"] == int(ending == "invalid")
    assert record["errors_omitted"] == 0
    assert record["triggers"] == record["sensors"] == 1
    assert record["expressions"] == result.expressions_checked
    assert record["unresolved"] == result.expressions_unresolved
    assert record["syntax_steps"] == result.syntax_steps
    assert record["checker_steps"] == result.checker_steps
    assert marker not in output
    assert all(marker not in error.message for error in result.diagnostics.errors)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_sensor_trigger_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = triggers(1, "unknown")
    expected = validate_sensor_trigger_expressions(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_sensor_trigger_expressions(document)
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
