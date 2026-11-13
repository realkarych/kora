import traceback
from dataclasses import replace

import pytest

from kora.db.claim_source_values import ClaimSourcesRejected, prepare_claim_sources
from kora.db.connection import Database
from kora.engine.claim_sources import (
    decode_claim_sources,
    insert_claim_attempt,
    recheck_claim_sources,
)
from kora.engine.locks import TransitionLocks
from kora.ir.json import JsonObject
from kora.observability import Level, LoggingSettings
from tests.db.closing import ConnectionLedger
from tests.db.seed import START
from tests.engine.test_claim_sources import (
    ALLOWANCE,
    LIMITS,
    PRIVATE,
    candidate,
    read,
    seed,
    source_values,
)
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
@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
async def test_claim_source_diagnostics_exclude_content_and_failed_writes(
    database: Database, configure: Configure, pipe: Pipe, level: Level
) -> None:
    binding, values = await candidate(database)
    sources: JsonObject = {**source_values(), "handle": {"content": PRIVATE}}
    prepared = prepare_claim_sources(sources, {name: ALLOWANCE for name in sources})
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    async def duplicate_insert() -> None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(binding.run.run_id)
            await insert_claim_attempt(
                writer, locks, binding, values, prepared, LIMITS, START
            )

    await duplicate_insert()
    with pytest.raises(ClaimSourcesRejected) as failure:
        await duplicate_insert()
    acquired = await read(database, binding)
    assert acquired is not None
    malformed = replace(
        acquired, values=(PRIVATE.encode() + b"\xff", *acquired.values[1:])
    )
    with pytest.raises(ClaimSourcesRejected) as decoding:
        decode_claim_sources(malformed)
    finished_output(diagnostics, pipe)
    records = pipe.reader.records()

    assert [
        record["created"]
        for record in records
        if record["event"] == "engine.claim_sources.prepared"
    ] == ([True] if level is Level.DEBUG else [])
    assert [
        record["acquired"]
        for record in records
        if record["event"] == "db.claim_sources.acquired"
    ] == ([True] if level is Level.DEBUG else [])
    for caught in (failure.value, decoding.value):
        assert str(caught) == "claim sources are unavailable"
        assert caught.__cause__ is caught.__context__ is None
        assert PRIVATE not in "".join(traceback.format_exception(caught))
    assert PRIVATE.encode() not in pipe.reader.data
    assert binding.attempt_id.encode() not in pipe.reader.data
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_claim_source_progress_survives_unavailable_sink(
    database: Database, configure: Configure, pipe: Pipe, broken: bool
) -> None:
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    binding = await seed(database)
    acquired = await read(database, binding)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert acquired is not None
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_claim_source_recheck_native_failure_keeps_scope_private(
    database: Database,
) -> None:
    binding = await seed(database)
    acquired = await read(database, binding)
    assert acquired is not None
    async with database.transaction() as writer:
        await writer.exec_driver_sql("DROP TABLE attempt_claim_read_admission")

    async def recheck() -> bool:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(binding.run.run_id)
            return await recheck_claim_sources(writer, locks, acquired, LIMITS)

    with pytest.raises(ClaimSourcesRejected) as failure:
        await recheck()

    assert str(failure.value) == "claim sources are unavailable"
    assert failure.value.__cause__ is failure.value.__context__ is None
    assert binding.attempt_id not in "".join(traceback.format_exception(failure.value))
    assert database.units_in_flight() == 0
