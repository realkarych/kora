import pytest

from kora.ir import validate_graph_links
from kora.observability import LoggingSettings
from tests.ir.graph_link_cases import model, node, ref
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


def test_graph_link_logging_is_content_free(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "private_graph_content"
    document = model(
        {
            marker: node(
                inputs={"value": ref(marker + "_absent")},
                source={"file": marker + ".py", "line": 1},
            )
        },
        namespace=marker,
    )
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings())

    result = validate_graph_links(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.graph_links.completed"
    ]
    assert not result.links_valid
    assert len(records) == 1
    assert records[0]["level"] == "INFO"
    assert records[0]["component"] == "ir.validation"
    assert records[0]["links_valid"] is False
    assert records[0]["errors"] == 1
    assert records[0]["errors_omitted"] == 0
    assert records[0]["nodes"] == 1
    assert marker not in output
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_graph_link_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = model({"timer": node(inputs={"value": ref("absent")})})
    expected = validate_graph_links(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_graph_links(document)
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
