import pytest

from kora.ir import validate_wait_expressions
from kora.observability import Level, LoggingSettings, bound_scope
from tests.ir.approval_input_cases import unavailable_inputs
from tests.ir.inline_schema_cases import document_for
from tests.ir.wait_expression_cases import wait
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("ending", ["valid", "invalid", "syntax", "context", "schema"])
def test_wait_expression_logging_excludes_content(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], ending: str
) -> None:
    marker = "private_wait_expression_content"
    text = {
        "valid": "params." + marker,
        "invalid": marker,
        "syntax": marker + " &&",
        "context": "inputs." + marker,
        "schema": "params." + marker,
    }[ending]
    document = document_for(
        params={"$ref": "https://example.invalid/" + marker}
        if ending == "schema"
        else {},
        nodes={
            "wait": wait(
                text,
                inputs=unavailable_inputs(),
                source={"file": marker + ".py", "line": 3},
            )
        },
    )
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    with bound_scope(request_id="req_wait_typing"):
        result = validate_wait_expressions(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.wait_expressions.completed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record["request_id"] == "req_wait_typing"
    assert record["level"] == "INFO"
    assert record["component"] == "ir.validation"
    assert record["wait_expressions_valid"] is (ending == "valid")
    assert record["wait_expressions_complete"] is (ending in {"valid", "invalid"})
    assert record["errors"] == int(ending == "invalid")
    assert record["errors_omitted"] == 0
    assert record["nodes"] == record["waits"] == 1
    assert record["expressions_seen"] == 1
    assert record["expressions_checked"] == result.expressions_checked
    assert record["syntax_unresolved"] == result.syntax_unresolved
    assert record["context_unresolved"] == result.context_unresolved
    assert record["schema_unresolved"] == result.schema_unresolved
    assert record["schema_steps"] == result.schema_steps
    assert record["checker_steps"] == result.checker_steps
    assert marker not in output
    assert all(marker not in error.message for error in result.diagnostics.errors)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_wait_expression_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = document_for(nodes={"wait": wait("unknown")})
    expected = validate_wait_expressions(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_wait_expressions(document)
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
