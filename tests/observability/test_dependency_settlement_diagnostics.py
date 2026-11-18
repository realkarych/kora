import pytest

from kora.db.connection import Database
from kora.engine.dependency_settlement import DependencySettlementState
from kora.observability import Level, LoggingSettings
from tests.db.closing import ConnectionLedger
from tests.engine.dependency_settlement_support import dependency_case
from tests.engine.test_readiness import PRIVATE
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
async def test_dependency_diagnostics_exclude_graph_and_predecessor_content(
    configure: Configure, pipe: Pipe, database: Database, level: Level
) -> None:
    case = await dependency_case(database)
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    outcome = await case.settle(database)
    replay = await case.settle(database)
    finished_output(diagnostics, pipe)
    records = pipe.reader.records()

    assert outcome is DependencySettlementState.SETTLED
    assert replay is DependencySettlementState.UNCHANGED
    assert [
        record["outcome"]
        for record in records
        if record["event"] == "engine.dependency_settlement.prepared"
    ] == (["settled", "unchanged"] if level is Level.DEBUG else [])
    for private in (
        PRIVATE,
        case.promotion.run_id,
        case.promotion.pin.execution_contract_digest,
        case.node_ids["approval"],
    ):
        assert private.encode() not in pipe.reader.data
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_dependency_settlement_survives_unavailable_diagnostic_sink(
    configure: Configure, pipe: Pipe, database: Database, broken: bool
) -> None:
    case = await dependency_case(database)
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    outcome = await case.settle(database)
    replay = await case.settle(database)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert outcome is DependencySettlementState.SETTLED
    assert replay is DependencySettlementState.UNCHANGED
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
