import pytest

from kora.db.connection import Database
from kora.engine.locks import (
    GraphPublicationDigestReadFailed,
    Scope,
    ScopeKind,
    TransitionLocks,
)
from kora.observability import Level, LoggingSettings
from tests.engine.lock_support import RUN_A, lock_inventory
from tests.engine.test_graph_publication_target import publication_locks, seed_target
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [Level.DEBUG, Level.ERROR])
async def test_engine_lock_diagnostics_are_content_free(
    database: Database,
    configure: Configure,
    pipe: Pipe,
    level: Level,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_name = "private-target-never-in-diagnostics"
    scope = Scope(ScopeKind.TARGET, ("private-namespace", private_name))
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_run(RUN_A)
        await locks.acquire_scopes((scope,))
        locks.require(RUN_A, (scope,))
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "engine.locks.acquired"
    ]
    assert len(records) == (2 if level is Level.DEBUG else 0)
    assert [record["scope_keys"] for record in records] == (
        [0, 1] if level is Level.DEBUG else []
    )
    assert all(record["level"] == "DEBUG" for record in records)
    assert private_name not in output
    assert "private-namespace" not in output
    assert RUN_A not in output
    assert capsys.readouterr().out == ""
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_engine_locks_release_with_unavailable_sink(
    database: Database, configure: Configure, pipe: Pipe, broken: bool
) -> None:
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()

    for _ in range(256):
        async with database.transaction() as connection:
            locks = TransitionLocks(connection)
            await locks.acquire_run(RUN_A)
            await locks.acquire_scopes((Scope(ScopeKind.GLOBAL),))
            locks.require(RUN_A)
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
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_graph_publication_digest_failure_is_content_free_at_debug(
    database: Database, configure: Configure, pipe: Pipe
) -> None:
    binding, selected = await seed_target(database)
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    async def fail_digest_read() -> None:
        async with database.transaction() as writer:
            locks = await publication_locks(writer, binding, selected)
            assert (
                await locks.read_graph_publication_target(writer, binding, selected)
                == selected
            )
            await writer.exec_driver_sql(
                "ALTER TABLE graph_version RENAME TO unavailable_graph_version"
            )
            await locks.read_graph_publication_digest_match(writer, binding, selected)

    with pytest.raises(GraphPublicationDigestReadFailed):
        await fail_digest_read()
    output = finished_output(diagnostics, pipe)

    assert '"event":"engine.locks.acquired"' in output
    assert '"event":"db.transaction.failed"' in output
    for private in (
        selected,
        binding.namespace_id,
        binding.preparation_id,
        binding.graph_name,
        binding.deployment_digest,
        "SELECT",
        "unavailable_graph_version",
    ):
        assert private not in output
    assert await lock_inventory(database) == []
