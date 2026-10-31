import pytest

from kora.ir import validate_approval_defaults
from kora.ir.json import JsonObject
from kora.observability import Level, LoggingSettings
from tests.ir.approval_default_cases import decision_form, gate
from tests.ir.inline_schema_cases import document_for
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("ending", ["valid", "invalid", "unresolved"])
def test_approval_default_logging_is_content_free(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], ending: str
) -> None:
    marker = "private_approval_schema_content"
    form: JsonObject = decision_form(
        {"enum": [marker] if ending == "invalid" else ["approve"]}
    )
    form["description"] = marker
    form["$defs"] = {marker: {"const": marker}}
    if ending == "unresolved":
        form = {"$ref": "https://example.invalid/" + marker}
    document = document_for(
        nodes={"gate": gate(form, source={"file": marker + ".py", "line": 1})}
    )
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    result = validate_approval_defaults(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.approval_defaults.completed"
    ]
    assert result.approval_defaults_valid is (ending == "valid")
    assert len(records) == 1
    record = records[0]
    assert record["level"] == "INFO"
    assert record["component"] == "ir.validation"
    assert record["approval_defaults_valid"] is result.approval_defaults_valid
    assert record["errors"] == int(ending == "invalid")
    assert record["errors_omitted"] == record["definitions"] == 0
    assert record["nodes"] == record["human_inputs"] == 1
    assert record["predicates"] == record["expressions"] == int(ending != "unresolved")
    assert record["unresolved"] == result.predicates_unresolved
    assert record["schema_steps"] == result.schema_steps
    assert record["checker_steps"] == result.checker_steps
    assert marker not in output
    assert all(marker not in error.message for error in result.diagnostics.errors)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_approval_default_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = document_for(nodes={"gate": gate({})})
    expected = validate_approval_defaults(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_approval_defaults(document)
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
