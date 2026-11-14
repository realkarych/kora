import traceback

import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.grant_history import GrantHistoryRejected
from kora.observability import Level, LoggingSettings
from tests.api.deployment_cases import evaluator
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import claim_case
from tests.engine.heartbeat_support import heartbeat, heartbeat_body
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
@pytest.mark.asyncio
async def test_heartbeat_diagnostics_exclude_content_and_native_failure(
    database: Database,
    evaluator: Evaluator,
    configure: Configure,
    pipe: Pipe,
    level: Level,
) -> None:
    case = await claim_case(database, evaluator)
    claimed = await case.claim(database)
    assert claimed.delivery is not None
    body = heartbeat_body(claimed.delivery.system)
    clock = AuthorityClock()
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    first = await heartbeat(database, case, clock, body)
    replay = await heartbeat(database, case, clock, body)
    async with database.transaction() as writer:
        await writer.exec_driver_sql("DROP TABLE attempt_dispatcher")
    with pytest.raises(GrantHistoryRejected) as caught:
        await heartbeat(database, case, clock, body)
    finished_output(diagnostics, pipe)

    assert first == replay
    assert [
        record["events"]
        for record in pipe.reader.records()
        if record["event"] == "engine.heartbeat.prepared"
    ] == ([0, 0] if level is Level.DEBUG else [])
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert "attempt_dispatcher" not in "".join(traceback.format_exception(caught.value))
    assert b"attempt_dispatcher" not in pipe.reader.data
    assert case.binding.attempt_id.encode() not in pipe.reader.data
    assert case.registration.name.encode() not in pipe.reader.data
    assert b"Summarize the repository" not in pipe.reader.data
    frame = caught.value.__traceback__
    while frame is not None:
        if frame.tb_frame.f_code.co_name == "heartbeat_attempt":
            assert {"context", "body", "principal"}.isdisjoint(frame.tb_frame.f_locals)
        frame = frame.tb_next
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.parametrize("broken", [False, True])
@pytest.mark.asyncio
async def test_heartbeat_commits_with_blocked_or_broken_diagnostic_sink(
    database: Database,
    evaluator: Evaluator,
    configure: Configure,
    pipe: Pipe,
    broken: bool,
) -> None:
    case = await claim_case(database, evaluator)
    claimed = await case.claim(database)
    assert claimed.delivery is not None
    body = heartbeat_body(claimed.delivery.system)
    clock = AuthorityClock()
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    first = await heartbeat(database, case, clock, body)
    replay = await heartbeat(database, case, clock, body)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert first == replay
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
