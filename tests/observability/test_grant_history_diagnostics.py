import time
import traceback

import pytest

from kora.db.connection import Database
from kora.db.grant_history import GrantHistoryRejected
from kora.engine.grant_history import acquire_history, append_grant
from kora.engine.locks import TransitionLocks
from kora.observability import Level, LoggingSettings
from kora.protocols.identity import GrantBinding
from tests.db.closing import ConnectionLedger
from tests.db.seed import START
from tests.engine.test_grant_history import LIMITS, acquired, append, grant, seed
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)

PRIVATE = "private-history-process-value"


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
async def test_history_native_refusal_excludes_process_and_sql(
    database: Database,
    configure: Configure,
    pipe: Pipe,
    level: Level,
) -> None:
    scope = await seed(database)
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))
    original = grant(scope, 1, process=True).model_dump(mode="json")
    original["lease_epoch"] = 6
    original["process"]["host_id"] = PRIVATE
    duplicate = GrantBinding.model_validate(original)
    await append(database, scope, 1)
    ledger = ConnectionLedger(database)

    async def refused_insert() -> None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(scope.run_id)
            await append_grant(
                writer, locks, scope, duplicate, "execute", START, LIMITS
            )

    with pytest.raises(GrantHistoryRejected) as duplicate_error:
        await refused_insert()
    await acquired(database, scope)
    with pytest.raises(GrantHistoryRejected) as deadline_error:
        await acquire_history(database, scope, LIMITS, deadline=time.monotonic() - 1)
    finished_output(diagnostics, pipe)

    for error in (duplicate_error.value, deadline_error.value):
        assert error.__cause__ is error.__context__ is None
        assert str(error) == "grant history is unavailable"
        assert PRIVATE not in "".join(traceback.format_exception(error))
        assert "INSERT INTO" not in "".join(traceback.format_exception(error))
    assert PRIVATE.encode() not in pipe.reader.data
    assert scope.attempt_id.encode() not in pipe.reader.data
    assert [
        record["acquired"]
        for record in pipe.reader.records()
        if record["event"] == "engine.grant_history.acquired"
    ] == ([True, False] if level is Level.DEBUG else [])
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_history_settlement_survives_unavailable_sink(
    database: Database,
    configure: Configure,
    pipe: Pipe,
    broken: bool,
) -> None:
    scope = await seed(database)
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    result = await append(database, scope, 1)
    await acquired(database, scope)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert result is not None
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
