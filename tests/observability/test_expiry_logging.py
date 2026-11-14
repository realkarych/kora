from dataclasses import replace

import pytest
from sqlalchemy import insert

from kora.conformance.capture import captured
from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import event
from kora.engine.expiry import expire_run
from kora.engine.expiry_context import ExpiryFailed, ExpiryState
from kora.engine.locks import COMPONENT
from kora.observability import Level, LoggingSettings
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder, fixed_randomness
from tests.engine.expiry_support import ExecutionCase, execution_case
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    wait_until,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_expiry_diagnostics_exclude_private_metadata(
    database: Database, failure: bool
) -> None:
    case = await execution_case(database, timeout="0s")
    if failure:
        case.promotion.clock.advance(1)
    ledger = ConnectionLedger(database)
    generator = IdentifierGenerator(case.promotion.clock.now, fixed_randomness(1000))
    if failure:
        collision = IdentifierGenerator(
            case.promotion.clock.now, fixed_randomness(1000)
        ).new()
        async with database.transaction() as writer:
            await writer.execute(
                insert(event).values(
                    id=collision,
                    namespace_id=case.promotion.scope.namespace_id,
                    run_id=case.promotion.run_id,
                    kind="run.created",
                    actor={},
                    payload={"private": "expiry-private-value"},
                    at=case.promotion.clock.now(),
                    **Seeder(writer).stamps(),
                )
            )
    before = await case.promotion.snapshot(database)

    with captured(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG),
        "expiry-test",
        case.promotion.clock.now,
    ) as capture:
        if failure:
            with pytest.raises(ExpiryFailed, match="execution authority expiry failed"):
                await failing_expiry(database, case.promotion.run_id, generator, case)
        else:
            async with database.transaction() as writer:
                assert (
                    await expire_run(await case.context(writer)) is ExpiryState.EXPIRED
                )
        records = capture.finish()
    after = await case.promotion.snapshot(database)

    assert all(
        value not in repr(records)
        for value in (
            case.promotion.run_id,
            case.attempt_id,
            case.domain_id,
            "expiry-private-value",
            "original",
            "publication",
        )
    )
    actions = [
        row["action"] for row in records if row["event"] == "engine.expiry.prepared"
    ]
    assert actions == ([] if failure else ["run_guard", "run_expiry"])
    if failure:
        assert after == before
    assert ledger.outstanding == database.units_in_flight() == 0


async def failing_expiry(
    database: Database, run_id: str, generator: IdentifierGenerator, case: ExecutionCase
) -> None:
    async with database.transaction() as writer:
        context = replace(
            await case.context(writer), run_id=run_id, identifiers=generator
        )
        await expire_run(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_expiry_survives_unavailable_diagnostic_sink(
    database: Database, configure: Configure, pipe: Pipe, broken: bool
) -> None:
    diagnostics = configure(
        LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536),
        pipe.write_end,
    )
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)
    case = await execution_case(database, timeout="0s")

    async with database.transaction() as writer:
        result = await expire_run(await case.context(writer))
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()
    saved, _, _ = await case.promotion.snapshot(database)

    assert result is ExpiryState.EXPIRED
    assert saved["status"] == "failed"
    assert snapshot.dropped_total() > 0
    assert drained
    assert not diagnostics.writer_is_alive()
    assert database.units_in_flight() == 0
