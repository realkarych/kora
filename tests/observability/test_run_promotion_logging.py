import pytest

from kora.db.connection import Database
from kora.engine.run_promotion import PromotionOutcome
from kora.observability import Level, LoggingSettings
from tests.engine.promotion_support import promotion_case
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    wait_until,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_promotion_survives_unavailable_diagnostic_sink(
    database: Database, configure: Configure, pipe: Pipe, broken: bool
) -> None:
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    case = await promotion_case(database)
    result = await case.promote(database)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()
    saved, nodes, events = await case.snapshot(database)

    assert result is PromotionOutcome.APPLIED
    assert saved["status"] == "running"
    assert len(nodes) == 3
    assert len(events) == 1
    assert snapshot.dropped_total() > 0
    assert drained
    assert not diagnostics.writer_is_alive()
    assert database.units_in_flight() == 0
