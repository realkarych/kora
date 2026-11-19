import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.attempt_claims import ClaimOutcome
from kora.observability import Level, LoggingSettings
from tests.api.deployment_cases import evaluator
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import claim_case
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    wait_until,
)

__all__ = ["evaluator"]


@pytest.mark.parametrize("broken", [False, True])
@pytest.mark.asyncio
async def test_claim_transition_survives_unavailable_sink(
    database: Database,
    evaluator: Evaluator,
    configure: Configure,
    pipe: Pipe,
    broken: bool,
) -> None:
    case = await claim_case(database, evaluator)
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    first = await case.claim(database)
    replay = await case.claim(database)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert first.outcome is ClaimOutcome.GRANTED
    assert replay.outcome is ClaimOutcome.REPLAYED
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
