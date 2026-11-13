import asyncio
import traceback

import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database
from kora.db.tables import namespace, price_table
from kora.engine.claim_snapshot import acquire_claim_snapshot, prepare_claim_snapshot
from kora.observability import Level, LoggingSettings
from tests.api.deployment_cases import evaluator
from tests.db.closing import ConnectionLedger
from tests.engine.test_claim_snapshot import LIMITS, SELECTION, acquire, seed
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)

__all__ = ["evaluator"]
PRIVATE = "private-claim-snapshot-value"


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
@pytest.mark.parametrize("malformed", ["price", "policy"])
async def test_claim_snapshot_diagnostics_exclude_malformed_sources(
    database: Database,
    evaluator: Evaluator,
    configure: Configure,
    pipe: Pipe,
    level: Level,
    malformed: str,
) -> None:
    binding, pin = await seed(database, evaluator)
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    successful = await acquire(database, binding)
    prepare_claim_snapshot(successful, pin, SELECTION, LIMITS)
    async with database.transaction() as writer:
        if malformed == "price":
            await writer.execute(
                update(price_table)
                .where(price_table.c.id == binding.price_table_id)
                .values(prices={PRIVATE: {}})
            )
        else:
            await writer.execute(
                update(namespace)
                .where(namespace.c.id == binding.run.namespace_id)
                .values(policy={"require_protected_execution": PRIVATE})
            )
    acquired = await acquire(database, binding)
    with pytest.raises(ClaimSourcesRejected) as decoding:
        prepare_claim_snapshot(acquired, pin, SELECTION, LIMITS)
    with pytest.raises(ClaimSourcesRejected) as deadline:
        await acquire_claim_snapshot(
            database, binding, LIMITS, deadline=asyncio.get_running_loop().time() - 1.0
        )
    finished_output(diagnostics, pipe)

    records = pipe.reader.records()
    assert [
        record["acquired"]
        for record in records
        if record["event"] == "engine.claim_snapshot.acquired"
    ] == ([True, True, False] if level is Level.DEBUG else [])
    for caught in (decoding.value, deadline.value):
        assert caught.__cause__ is caught.__context__ is None
        assert str(caught) == "claim sources are unavailable"
        assert PRIVATE not in "".join(traceback.format_exception(caught))
    assert PRIVATE.encode() not in pipe.reader.data
    assert binding.attempt_id.encode() not in pipe.reader.data
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_snapshot_native_failure_is_content_free(
    database: Database, evaluator: Evaluator, configure: Configure, pipe: Pipe
) -> None:
    binding, _ = await seed(database, evaluator)
    async with database.transaction() as writer:
        await writer.exec_driver_sql("DROP TABLE price_table")
    ledger = ConnectionLedger(database)
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    with pytest.raises(ClaimSourcesRejected) as failed:
        await acquire(database, binding)
    finished_output(diagnostics, pipe)

    assert failed.value.__cause__ is failed.value.__context__ is None
    assert "price_table" not in "".join(traceback.format_exception(failed.value))
    assert b"price_table" not in pipe.reader.data
    assert binding.attempt_id.encode() not in pipe.reader.data
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_claim_snapshot_survives_unavailable_sink(
    database: Database,
    evaluator: Evaluator,
    configure: Configure,
    pipe: Pipe,
    broken: bool,
) -> None:
    binding, pin = await seed(database, evaluator)
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    acquired = await acquire(database, binding)
    result = prepare_claim_snapshot(acquired, pin, SELECTION, LIMITS)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert result.sources.binding == binding
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
