import pytest

from kora.ir import validate_fan_out_types
from kora.observability import Level, LoggingSettings
from tests.ir.export_type_cases import export_body
from tests.ir.fan_out_type_cases import fan_out_node, fan_outs
from tests.ir.inline_schema_cases import document_for
from tests.ir.type_declaration_cases import custom
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("invalid", [False, True])
def test_fan_out_type_logging_is_content_free(
    configure: Configure,
    pipe: Pipe,
    capsys: pytest.CaptureFixture[str],
    invalid: bool,
) -> None:
    markers = ("private_type", "private_schema", "private_behavior", "private_source")
    first, second = markers[0] + "/A", markers[0] + "/B"
    contract = custom()
    contract["schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "description": markers[1],
        "$ref": "https://example.invalid/" + markers[1],
    }
    contract["behavior"] = dict.fromkeys(
        ("validation", "digest_fields", "summary"), markers[2]
    )
    document = document_for(
        contracts={first: contract, second: custom()},
        nodes={
            "fan": fan_out_node(
                first if invalid else f"List[{first}]",
                f"List[List[{second if invalid else first}]]",
                source={"file": markers[3] + ".py", "line": 7},
            )
        },
        bodies={"body": export_body(f"List[{first}]", f"List[{first}]")},
    )
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    result = validate_fan_out_types(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.fan_out_types.completed"
    ]
    assert result.fan_out_types_valid is not invalid
    assert len(records) == 1
    record = records[0]
    assert record["level"] == "INFO"
    assert record["component"] == "ir.validation"
    assert record["fan_out_types_valid"] is not invalid
    assert record["errors"] == 2 * int(invalid)
    assert record["errors_omitted"] == 0
    assert record["contracts"] == record["types_indexed"] == record["nodes"] == 2
    assert (
        record["definitions"]
        == record["fan_outs"]
        == record["collections"]
        == record["mapped_inputs_checked"]
        == record["collections_compared"]
        == 1
    )
    assert record["mapped_inputs_unresolved"] == record["collections_unresolved"] == 0
    assert all(marker not in output for marker in markers)
    assert all(
        marker not in error.message
        for marker in markers
        for error in result.diagnostics.errors
    )
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_fan_out_type_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = fan_outs(1, invalid=True, list_depth=2)
    expected = validate_fan_out_types(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_fan_out_types(document)
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
