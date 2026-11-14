import traceback

import pytest
from sqlalchemy import select

from kora.cel_eval.thread import Evaluator
from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database
from kora.db.tables import attempt
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
    configured_with_reader,
    finished_output,
    wait_until,
)

__all__ = ["evaluator"]


@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
@pytest.mark.asyncio
async def test_claim_transition_diagnostics_exclude_content_and_native_refusal(
    database: Database,
    evaluator: Evaluator,
    configure: Configure,
    pipe: Pipe,
    level: Level,
) -> None:
    case = await claim_case(database, evaluator)
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    first = await case.claim(database)
    replay = await case.claim(database)
    prepared = await case.prepare(database)
    async with database.transaction() as writer:
        await writer.exec_driver_sql("DROP TABLE attempt_dispatcher")

    async def refused() -> None:
        async with database.transaction() as writer:
            await case.apply(writer, prepared)

    with pytest.raises(ClaimSourcesRejected) as caught:
        await refused()
    finished_output(diagnostics, pipe)

    assert first.outcome is ClaimOutcome.GRANTED
    assert replay.outcome is ClaimOutcome.REPLAYED
    assert [
        record["outcome"]
        for record in pipe.reader.records()
        if record["event"] == "engine.attempt_claim.prepared"
    ] == (["granted", "replayed"] if level is Level.DEBUG else [])
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert "attempt_dispatcher" not in "".join(traceback.format_exception(caught.value))
    assert b"attempt_dispatcher" not in pipe.reader.data
    assert case.binding.attempt_id.encode() not in pipe.reader.data
    assert case.registration.name.encode() not in pipe.reader.data
    assert b"Summarize the repository" not in pipe.reader.data
    frame = caught.value.__traceback__
    while frame is not None:
        if frame.tb_frame.f_code.co_name == "claim_attempt":
            assert {"context", "query", "principal", "prepared"}.isdisjoint(
                frame.tb_frame.f_locals
            )
        frame = frame.tb_next
    async with database.read_transaction() as reader:
        assert await reader.scalar(select(attempt.c.lease_epoch)) == 1
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


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
