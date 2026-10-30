import pytest

from kora.ir import validate_secret_aliases
from kora.observability import Level, LoggingSettings, bound_scope, current_context
from tests.ir.secret_alias_cases import all_secret_sites, secret_document, webhook
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.parametrize("invalid", [False, True])
def test_secret_alias_logging_is_content_free(
    configure: Configure,
    pipe: Pipe,
    capsys: pytest.CaptureFixture[str],
    invalid: bool,
) -> None:
    alias = "PRIVATE_ALIAS"
    markers = (alias, "private_reference", "private_environment", "private_server")
    nodes = all_secret_sites(alias)
    nodes["subject"] = {
        "kind": "agent",
        "outputs": {"session": {"type": "AgentSession"}},
        "config": {
            "harness": "claude",
            "prompt": markers[2],
            "workspace": {"repo": ""},
            "env": {"TOKEN": markers[2]},
            "mcp_servers": {
                markers[3]: {
                    "type": "http",
                    "env": {markers[2]: markers[2]},
                    "secret_env": {markers[2]: alias},
                }
            },
        },
    }
    document = secret_document(
        nodes,
        secrets={
            "aliases": {"OTHER" if invalid else alias: "secret://unknown/" + markers[1]}
        },
        triggers=[webhook(alias)],
    )
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    with bound_scope(namespace_id="authorized_namespace", request_id="request"):
        result = validate_secret_aliases(document)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.secret_aliases.completed"
    ]
    assert result.secret_aliases_valid is not invalid
    assert len(records) == 1
    record = records[0]
    assert record["level"] == "INFO"
    assert record["component"] == "ir.validation"
    assert record["namespace_id"] == "authorized_namespace"
    assert record["request_id"] == "request"
    assert current_context() == {}
    assert record["secret_aliases_valid"] is not invalid
    assert record["errors"] == 6 * int(invalid)
    assert record["errors_omitted"] == record["definitions"] == 0
    assert record["nodes"] == 3
    assert record["triggers"] == 1
    assert record["mcp_servers"] == 5
    assert record["references"] == 6
    assert all(marker not in output for marker in markers)
    assert all(marker not in repr(result) for marker in markers)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("broken", [False, True])
def test_secret_alias_result_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, broken: bool
) -> None:
    document = secret_document(all_secret_sites("ABSENT"))
    expected = validate_secret_aliases(document)
    settings = LoggingSettings(queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(2000):
        actual = validate_secret_aliases(document)
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
