import pytest

from kora.ir import validate_tool_policies
from kora.observability import Level, LoggingSettings
from tests.ir.inline_schema_cases import document_for
from tests.ir.tool_policy_cases import policy_agent, policy_document
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("invalid", [False, True])
def test_tool_policy_logging_is_content_free(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], invalid: bool
) -> None:
    marker = "private_policy_content"
    pattern = "Bash(" + marker + ("()" if invalid else ")")
    document = document_for(
        nodes={
            marker: policy_agent(
                {"deny": [pattern], "x-private": marker},
                source={"file": marker + ".py", "line": 1},
            )
        }
    )
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    result = validate_tool_policies(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.tool_policies.completed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record["level"] == "INFO"
    assert record["component"] == "ir.validation"
    assert record["tool_policies_valid"] is not invalid
    assert record["errors"] == int(invalid)
    assert record["errors_omitted"] == record["definitions"] == 0
    assert record["nodes"] == record["policies"] == record["patterns"] == 1
    assert result.tool_policies_valid is not invalid
    assert marker not in output
    assert all(marker not in error.message for error in result.diagnostics.errors)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_tool_policy_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = policy_document({"deny": ["StructuredOutput"]})
    expected = validate_tool_policies(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        assert validate_tool_policies(document) == expected
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
