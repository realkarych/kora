import pytest
from sqlalchemy import update

from kora.conformance.capture import captured
from kora.db.connection import Database
from kora.db.tables import node_instance
from kora.engine.locks import COMPONENT
from kora.engine.run_settlement import (
    RunSettlementFailed,
    RunSettlementState,
    settle_run,
)
from kora.observability import Level, LoggingSettings
from tests.engine.settlement_support import finished_run, settlement_context
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    wait_until,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_run_settlement_diagnostics_exclude_private_metadata(
    database: Database, failure: bool
) -> None:
    case = await finished_run(database, "failed", held=True)
    if failure:
        async with database.transaction() as writer:
            node = await writer.scalar(
                node_instance.select()
                .with_only_columns(node_instance.c.id)
                .where(node_instance.c.run_id == case.run_id)
                .limit(1)
            )
            await writer.execute(
                update(node_instance)
                .where(node_instance.c.id == node)
                .values(node_path="private-invalid-path")
            )
    before = await case.snapshot(database)

    async def finish() -> RunSettlementState:
        async with database.transaction() as writer:
            return await settle_run(await settlement_context(writer, case))

    with captured(
        COMPONENT, LoggingSettings(level=Level.DEBUG), "settlement-test", case.clock.now
    ) as capture:
        if failure:
            with pytest.raises(RunSettlementFailed):
                await finish()
        else:
            assert await finish() is RunSettlementState.SETTLED
        records = capture.finish()
    after = await case.snapshot(database)

    outcomes = [
        row["outcome"]
        for row in records
        if row["event"] == "engine.run_settlement.prepared"
    ]
    assert outcomes == ([] if failure else ["settled"])
    assert all(
        value not in repr(records)
        for value in (
            case.run_id,
            case.scope.namespace_id,
            "private-invalid-path",
            "held",
        )
    )
    if failure:
        assert before == after
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_run_settlement_survives_unavailable_diagnostic_sink(
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
    case = await finished_run(database, held=True)

    async with database.transaction() as writer:
        result = await settle_run(await settlement_context(writer, case))
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()
    saved, _, _ = await case.snapshot(database)

    assert result is RunSettlementState.SETTLED
    assert saved["status"] == "succeeded"
    assert saved["target_held_since"] is None
    assert snapshot.dropped_total() > 0
    assert drained
    assert not diagnostics.writer_is_alive()
    assert database.units_in_flight() == 0
