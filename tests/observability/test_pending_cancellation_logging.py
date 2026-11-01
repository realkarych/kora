import pytest

from kora.db.connection import Database
from kora.engine.cancellation import CancellationOutcome
from kora.observability import Level, LoggingSettings
from tests.engine.cancellation_support import (
    PRIVATE_PARAMS,
    PRIVATE_TRIGGER,
    pending_cancellation,
)
from tests.engine.lock_support import DeliberateRollback, lock_inventory
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "level", [Level.DEBUG, Level.INFO, Level.ERROR, Level.CRITICAL]
)
@pytest.mark.parametrize("rollback", [False, True])
async def test_pending_cancellation_diagnostics_do_not_claim_commit(
    database: Database,
    configure: Configure,
    pipe: Pipe,
    level: Level,
    rollback: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario = await pending_cancellation(database)
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    async def transition() -> None:
        async with database.transaction() as connection:
            await scenario.transition(connection, await scenario.acquire(connection))
            if rollback:
                raise DeliberateRollback()

    if rollback:
        with pytest.raises(DeliberateRollback):
            await transition()
    else:
        await transition()
    output = finished_output(diagnostics, pipe)
    after, events, _ = await scenario.snapshot()

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "engine.pending_cancellation.prepared"
    ]
    assert len(records) == (1 if level is Level.DEBUG else 0)
    assert all(
        record["level"] == "DEBUG" and record["wake_required"] is True
        for record in records
    )
    assert all(
        record["message"] == "pending cancellation prepared for commit"
        for record in records
    )
    assert after["status"] == ("pending" if rollback else "cancelled")
    assert len(events) == (0 if rollback else 1)
    for private in (
        PRIVATE_PARAMS,
        PRIVATE_TRIGGER,
        scenario.principal.owner,
        scenario.principal.token_id,
        scenario.run_id,
        scenario.namespace_id,
    ):
        assert private not in output
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_pending_cancellation_survives_unavailable_sink(
    database: Database,
    configure: Configure,
    pipe: Pipe,
    broken: bool,
) -> None:
    scenario = await pending_cancellation(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    result = await scenario.apply()
    repeated = await scenario.apply()
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()
    after, events, _ = await scenario.snapshot()

    assert result.outcome is CancellationOutcome.APPLIED
    assert repeated.outcome is CancellationOutcome.UNCHANGED
    assert after["status"] == "cancelled"
    assert len(events) == 1
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert await lock_inventory(database) == []
