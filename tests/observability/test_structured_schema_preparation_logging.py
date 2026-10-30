from dataclasses import fields, replace
from typing import cast

import pytest

from kora.ir.structured_schema_input import PreparedStructuredSchemaInput
from kora.ir.structured_schema_preparation import prepare_structured_schema_input
from kora.observability import Level, LoggingSettings
from tests.ir.structured_schema_cases import builtin, contract, preparation_input
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize(
    "status",
    ["prepared", "unsupported_binding", "unsupported_schema", "preparation_exhausted"],
)
def test_structured_output_preparation_diagnostics_are_content_free(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], status: str
) -> None:
    marker = "private_schema_content"
    declared = contract(
        {"description": marker, "$ref": "urn:kora:artifact:example:" + marker}
    )
    target = contract(False, "example/" + marker)
    cast("dict[str, object]", declared["source"])["location"] = {
        "file": marker,
        "extension": marker,
    }
    candidate = preparation_input({}, records=[builtin(), target, declared])
    if status == "unsupported_binding":
        candidate = replace(candidate, execution_semantics=marker)
    elif status == "unsupported_schema":
        cast("dict[str, object]", declared["schema"])["$ref"] = (
            "https://example.invalid/" + marker
        )
    elif status == "preparation_exhausted":
        cast("dict[str, object]", declared["schema"])["annotation"] = "x" * 1_048_576
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    try:
        raise RuntimeError(marker)
    except RuntimeError:
        result = prepare_structured_schema_input(candidate)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.structured_schema_preparation.completed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record["level"] == "DEBUG"
    assert record["component"] == "ir.validation"
    assert record["prepared"] is (status == "prepared")
    for reason in (
        "unsupported_binding",
        "unsupported_schema",
        "preparation_exhausted",
    ):
        assert record[reason] is (status == reason)
    if isinstance(result, PreparedStructuredSchemaInput):
        for field in fields(result.accounting):
            assert record[field.name] == getattr(result.accounting, field.name)
    else:
        assert result.reason == status
        for name in (
            "input_bytes",
            "input_nodes",
            "input_depth",
            "documents",
            "positions",
            "references",
            "work",
            "auxiliary_bytes",
            "auxiliary_entries",
        ):
            assert record[name] == 0
    assert marker not in output
    assert marker not in repr(result)
    assert marker not in repr(candidate)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True], ids=["full", "failed"])
def test_structured_output_preparation_survives_unavailable_diagnostic_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    candidate = preparation_input({"annotation": "private_schema_content"})
    expected = prepare_structured_schema_input(candidate)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    results_match = all(
        prepare_structured_schema_input(candidate) == expected
        for _ in range(settings.queue_records * 4)
    )
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert results_match
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
