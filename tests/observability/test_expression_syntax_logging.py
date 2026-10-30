import pytest

from kora.ir import validate_expression_syntax
from kora.observability import Level, LoggingSettings
from tests.ir.expression_syntax_cases import predicates
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("invalid", [False, True])
def test_expression_syntax_logging_is_content_free(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], invalid: bool
) -> None:
    marker = "private_expression_content"
    value = node(
        retry={"predicate": f"'{marker}'" + (" &&" if invalid else "")},
        inputs={
            "data": {
                "type": "StructuredOutput",
                "path": marker + ("." if invalid else ".value"),
            }
        },
        source={"file": marker + ".py", "line": 1},
    )
    graph = document_for(nodes={"subject": value})
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    result = validate_expression_syntax(graph)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.expression_syntax.completed"
    ]
    assert result.syntax_valid is not invalid
    assert len(records) == 1
    record = records[0]
    assert record["level"] == "INFO"
    assert record["component"] == "ir.validation"
    assert record["syntax_valid"] is not invalid
    assert record["errors"] == 2 * int(invalid)
    assert record["errors_omitted"] == record["definitions"] == record["triggers"] == 0
    assert record["nodes"] == record["expressions"] == record["paths"] == 1
    assert marker not in output
    assert all(marker not in error.message for error in result.diagnostics.errors)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_expression_syntax_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    graph = predicates(1, "true &&")
    expected = validate_expression_syntax(graph)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_expression_syntax(graph)
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
