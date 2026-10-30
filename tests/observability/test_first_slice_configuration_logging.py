from typing import cast

import pytest

from kora.ir import export_ir, validate_first_slice_configuration
from kora.ir.document import GraphDocument
from kora.observability import Level, LoggingSettings
from tests.ir.first_slice_configuration_cases import chain_document, chain_nodes
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("invalid", [False, True])
def test_first_slice_configuration_logging_is_content_free(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], invalid: bool
) -> None:
    markers = (
        "private_prompt",
        "private_repo",
        "private_source",
        "private_metadata",
        "private_hook",
    )
    nodes = chain_nodes()
    config = cast("dict[str, object]", nodes["agent"]["config"])
    config.update(prompt=markers[0], workspace={"repo": markers[1]})
    if invalid:
        config["hooks"] = {markers[4]: "private_hook_payload"}
    nodes["agent"]["source"] = {"file": markers[2] + ".py", "line": 7}
    data = export_ir(chain_document(nodes))
    data["meta"] = {"private": markers[3]}
    document = GraphDocument.model_validate(data)
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    result = validate_first_slice_configuration(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.first_slice_configuration.completed"
    ]
    assert result.authored_configuration_supported is not invalid
    assert len(records) == 1
    record = records[0]
    assert record["level"] == "INFO"
    assert record["component"] == "ir.validation"
    assert record["authored_configuration_supported"] is not invalid
    assert record["errors"] == int(invalid)
    assert (
        record["errors_omitted"]
        == record["definitions"]
        == record["triggers"]
        == record["ordering_unresolved"]
        == 0
    )
    assert record["nodes"] == record["ordering_checked"] == 3
    assert all(marker not in output for marker in markers)
    assert all(
        marker not in error.message and marker not in (error.hint or "")
        for marker in markers
        for error in result.diagnostics.errors
    )
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_first_slice_configuration_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = chain_document(runtime_fields={"provider_retry": {"max_wait": "1s"}})
    expected = validate_first_slice_configuration(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_first_slice_configuration(document)
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
