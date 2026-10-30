import pytest

from kora.ir import validate_type_declarations
from kora.observability import LoggingSettings
from tests.ir.graph_link_cases import node
from tests.ir.type_declaration_cases import custom, declarations, model
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("invalid", [False, True])
def test_type_declaration_logging_is_content_free(
    configure: Configure,
    pipe: Pipe,
    capsys: pytest.CaptureFixture[str],
    invalid: bool,
) -> None:
    marker = "private_type_content"
    contract = custom("private_type_content/Missing" if invalid else None)
    contract["schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "description": marker,
    }
    document = model(
        {marker + "/Value": contract},
        {
            marker: node(
                inputs={"value": {"type": marker + "/Value"}},
                source={"file": marker + ".py", "line": 1},
            )
        },
    )
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings())

    result = validate_type_declarations(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.type_declarations.completed"
    ]
    assert result.declarations_valid is not invalid
    assert len(records) == 1
    assert records[0]["level"] == "INFO"
    assert records[0]["component"] == "ir.validation"
    assert records[0]["declarations_valid"] is not invalid
    assert records[0]["errors"] == int(invalid)
    assert records[0]["errors_omitted"] == 0
    assert records[0]["declarations"] == 1
    assert records[0]["contracts"] == 1
    assert marker not in output
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_type_declaration_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = declarations(1, invalid=True)
    expected = validate_type_declarations(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_type_declarations(document)
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
