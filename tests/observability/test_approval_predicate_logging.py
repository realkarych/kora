import pytest

from kora.ir import validate_approval_response_predicates
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


@pytest.mark.parametrize("ending", ["valid", "invalid", "syntax", "context", "schema"])
def test_explicit_approval_logging_excludes_content(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], ending: str
) -> None:
    marker = "private_explicit_approval_content"
    form: JsonObject = decision_form({"enum": ["approve"]})
    form["description"] = marker
    form["$defs"] = {marker: {"const": marker}}
    predicate = {
        "valid": "response.decision == 'approve'",
        "invalid": "response.decision == '" + marker + "'",
        "syntax": "'" + marker + "' &&",
        "context": "inputs." + marker,
        "schema": "true",
    }[ending]
    if ending == "schema":
        form = {"$ref": "https://example.invalid/" + marker}
    document = document_for(
        nodes={
            "gate": gate(form, predicate, source={"file": marker + ".py", "line": 1})
        }
    )
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    result = validate_approval_response_predicates(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.approval_predicates.completed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record["level"] == "INFO"
    assert record["component"] == "ir.validation"
    assert record["approval_predicates_valid"] is (ending == "valid")
    assert record["approval_predicates_complete"] is (ending in {"valid", "invalid"})
    assert record["errors"] == int(ending == "invalid")
    assert record["errors_omitted"] == record["definitions"] == 0
    assert record["nodes"] == record["human_inputs"] == record["predicates_seen"] == 1
    assert record["predicates_checked"] == int(ending in {"valid", "invalid"})
    assert record["syntax_unresolved"] == int(ending == "syntax")
    assert record["context_unresolved"] == int(ending == "context")
    assert record["schema_unresolved"] == int(ending == "schema")
    assert record["schema_steps"] == result.schema_steps
    assert record["checker_steps"] == result.checker_steps
    assert marker not in output
    assert all(marker not in error.message for error in result.diagnostics.errors)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_explicit_approval_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = document_for(nodes={"gate": gate({}, "0")})
    expected = validate_approval_response_predicates(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_approval_response_predicates(document)
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
