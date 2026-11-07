import pytest

from kora.db.bootstrap import BootstrapOutcome, initialize
from kora.db.connection import Database
from kora.observability import Level, LoggingSettings
from tests.db.test_bootstrap import (
    clear_registry,
    clock,
    credentials,
    digest,
    refuse_credential,
    registry,
    seed_namespace,
    snapshot,
    text_of,
)
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


def fail_credential() -> str:
    raise RuntimeError("the credential source failed")


@pytest.mark.asyncio
async def test_bootstrap_logging_is_content_free(
    database: Database,
    configure: Configure,
    pipe: Pipe,
    capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )
    cli = "ab" * 32
    mcp = "cd" * 32
    await seed_namespace(database, "default")
    refused = await initialize(
        database, clock, credentials=credentials("e" * 64, "f" * 64)
    )
    await clear_registry(database)

    with pytest.raises(RuntimeError, match="the credential source failed"):
        await initialize(database, clock, credentials=fail_credential)
    created = await initialize(database, clock, credentials=credentials(cli, mcp))
    repeated = await initialize(database, clock, credentials=refuse_credential)
    output = finished_output(diagnostics, pipe)
    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "db.bootstrap.completed"
    ]
    stored = await snapshot(database)

    assert await registry(database) == (1, 2, 1)
    assert refused.outcome is BootstrapOutcome.REFUSED
    assert created.cli_credential == cli
    assert created.mcp_credential == mcp
    assert repeated.outcome is BootstrapOutcome.ALREADY_INITIALIZED
    assert [record["outcome"] for record in records] == [
        "refused",
        "initialized",
        "already_initialized",
    ]
    assert all(record["level"] == "INFO" for record in records)
    assert all(record["component"] == "db" for record in records)
    assert all(record["logger"] == "kora.db" for record in records)
    assert all(
        set(record)
        == {
            "schema_version",
            "timestamp",
            "level",
            "event",
            "message",
            "component",
            "logger",
            "instance_id",
            "outcome",
        }
        for record in records
    )
    private = [cli, mcp, digest(cli), digest(mcp)]
    for row in (*stored[0], *stored[1], *stored[2]):
        for key in ("id", "owner", "namespace_id", "cli_token_id", "mcp_token_id"):
            if key in row:
                private.append(text_of(row, key))
    assert all(value not in output for value in private)
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_bootstrap_result_survives_unavailable_sink(
    database: Database, configure: Configure, pipe: Pipe, broken: bool
) -> None:
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    result = await initialize(database, clock)
    if broken:
        for _ in range(64):
            again = await initialize(database, clock, credentials=refuse_credential)
            assert again.outcome is BootstrapOutcome.ALREADY_INITIALIZED
            assert again.cli_credential is None
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    else:
        emitted = 0
        while diagnostics.snapshot().dropped_total() == 0:
            emitted += 1
            assert emitted < 2000
            again = await initialize(database, clock, credentials=refuse_credential)
            assert again.outcome is BootstrapOutcome.ALREADY_INITIALIZED
            assert again.cli_credential is None
    diagnostic_snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()
    stored = await snapshot(database)
    cli = result.cli_credential
    mcp = result.mcp_credential

    assert cli is not None
    assert mcp is not None
    assert {text_of(row, "digest") for row in stored[1]} == {digest(cli), digest(mcp)}
    assert diagnostic_snapshot.dropped_total() > 0
    assert diagnostic_snapshot.queue_records <= settings.queue_records
    assert diagnostic_snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
