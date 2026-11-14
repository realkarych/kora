import pytest

from kora.db.connection import Database
from kora.engine.admission_wait import AdmissionWaitState
from kora.observability import Level, LoggingSettings
from tests.db.closing import ConnectionLedger
from tests.engine.admission_wait_support import admission_case
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
async def test_admission_wait_diagnostics_describe_preparation_without_content(
    configure: Configure, pipe: Pipe, database: Database, level: Level
) -> None:
    case = await admission_case(database)
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    async with database.transaction() as writer:
        state = await case.apply(await case.context(writer))
    [saved] = await case.saved_waits(database)
    async with database.transaction() as writer:
        refreshed = await case.apply(await case.context(writer), str(saved["id"]))
    finished_output(diagnostics, pipe)
    records = pipe.reader.records()

    assert state is AdmissionWaitState.CREATED
    assert refreshed is AdmissionWaitState.REFRESHED
    assert [
        record["outcome"]
        for record in records
        if record["event"] == "engine.admission_wait.prepared"
    ] == (["created", "refreshed"] if level is Level.DEBUG else [])
    for private in (
        case.promotion.run_id,
        case.binding.node_id,
        case.binding.node_path,
        str(saved["id"]),
        case.blocker,
        "observed_at",
    ):
        assert private.encode() not in pipe.reader.data


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_admission_wait_releases_writer_with_unavailable_sink(
    configure: Configure, pipe: Pipe, database: Database, broken: bool
) -> None:
    case = await admission_case(database)
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    async with database.transaction() as writer:
        state = await case.apply(await case.context(writer))
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert state is AdmissionWaitState.CREATED
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
