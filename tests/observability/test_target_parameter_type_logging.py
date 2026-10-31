import pytest

from kora.ir import validate_known_target_parameter_types
from kora.ir.json import JsonObject
from kora.observability import Level, LoggingSettings, bound_scope, current_context
from tests.ir.target_parameter_cases import parameter_schema, target_document
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("ending", ["valid", "invalid", "unresolved"])
def test_target_parameter_type_logging_is_content_free(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], ending: str
) -> None:
    name = "PRIVATE_PARAMETER"
    member: JsonObject = {"type": "integer" if ending == "invalid" else "string"}
    if ending == "unresolved":
        member = {"$ref": "urn:kora:artifact:PRIVATE_SCHEMA"}
    document = target_document(
        parameter_schema(member, name=name),
        parameter=name,
        source={"file": "PRIVATE_SOURCE.py", "line": 7},
    )
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    with bound_scope(namespace_id="authorized_namespace", request_id="request"):
        result = validate_known_target_parameter_types(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.target_parameter_types.completed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record["component"] == "ir.validation"
    assert record["level"] == "INFO"
    assert record["namespace_id"] == "authorized_namespace"
    assert record["request_id"] == "request"
    assert current_context() == {}
    assert (
        record["known_target_parameter_types_valid"]
        is result.known_target_parameter_types_valid
        is (ending == "valid")
    )
    assert record["errors"] == int(ending == "invalid")
    assert record["errors_omitted"] == 0
    assert record["targets"] == 1
    assert record["resolved"] == int(ending != "unresolved")
    assert record["unresolved"] == int(ending == "unresolved")
    assert record["schema_steps"] == result.schema_steps > 0
    assert all(
        marker not in output
        for marker in (name, "PRIVATE_SCHEMA", "PRIVATE_SOURCE", "integer", "string")
    )
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_target_parameter_type_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = target_document(parameter_schema({"type": "integer"}))
    expected = validate_known_target_parameter_types(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_known_target_parameter_types(document)
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
