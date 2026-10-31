import pytest

from kora.ir import validate_root_previous_attempt_refs
from kora.observability import Level, LoggingSettings, bound_scope, current_context
from tests.ir.graph_link_cases import model, node, ref
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("invalid", [False, True])
def test_root_previous_attempt_logging_is_content_free(
    configure: Configure,
    pipe: Pipe,
    capsys: pytest.CaptureFixture[str],
    invalid: bool,
) -> None:
    markers = ("private_node", "private_input", "private_source.py", "PRIVATE_PROMPT")
    document = model(
        {
            markers[0]: node(
                "agent",
                inputs={markers[1]: ref("$previous_attempt" if invalid else "$params")},
                source={"file": markers[2], "line": 7},
                config={
                    "harness": "claude",
                    "prompt": markers[3],
                    "workspace": {"repo": ""},
                },
            ),
            "other": node(),
        }
    )
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    with bound_scope(namespace_id="authorized_namespace", request_id="request"):
        result = validate_root_previous_attempt_refs(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.root_previous_attempts.completed"
    ]
    assert result.root_previous_attempt_refs_valid is not invalid
    assert len(records) == 1
    record = records[0]
    assert record["component"] == "ir.validation"
    assert record["level"] == "INFO"
    assert record["namespace_id"] == "authorized_namespace"
    assert record["request_id"] == "request"
    assert current_context() == {}
    assert record["root_previous_attempt_refs_valid"] is not invalid
    assert record["errors"] == int(invalid)
    assert record["errors_omitted"] == 0
    assert record["nodes"] == 2
    assert record["inputs"] == 1
    assert all(marker not in output for marker in markers)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_root_previous_attempt_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = model({"subject": node(inputs={"previous": ref("$previous_attempt")})})
    expected = validate_root_previous_attempt_refs(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_root_previous_attempt_refs(document)
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
