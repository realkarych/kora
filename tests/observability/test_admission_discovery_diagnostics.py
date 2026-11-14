import pytest

from kora.db.connection import Database
from kora.engine.admission_discovery import AdmissionDiscoveryFailed, AdmissionSweep
from kora.observability import Level, LoggingSettings
from tests.db.closing import ConnectionLedger
from tests.engine.promotion_support import promotion_case
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
async def test_admission_discovery_diagnostics_exclude_owner_and_content(
    database: Database, configure: Configure, pipe: Pipe, level: Level
) -> None:
    case = await promotion_case(database)
    await case.promote(database)
    sweep = AdmissionSweep(1)
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    async def unavailable() -> None:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "ALTER TABLE node_instance RENAME TO private_candidate_payload"
            )
            await sweep.next_page(writer)

    async with database.read_transaction() as reader:
        first = await sweep.next_page(reader)
        second = await sweep.next_page(reader)
    with pytest.raises(AdmissionDiscoveryFailed):
        await unavailable()
    finished_output(diagnostics, pipe)
    selected = [
        record
        for record in pipe.reader.records()
        if record["event"] == "engine.admission_discovery.selected"
    ]

    assert [(record["count"], record["wrapped"]) for record in selected] == (
        [(1, True), (1, False)] if level is Level.DEBUG else []
    )
    for private in (
        case.run_id,
        *first.node_ids,
        *second.node_ids,
        "private_candidate_payload",
        "input_bindings",
    ):
        assert private.encode() not in pipe.reader.data
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_admission_discovery_releases_reader_with_unavailable_sink(
    database: Database, configure: Configure, pipe: Pipe, broken: bool
) -> None:
    case = await promotion_case(database)
    await case.promote(database)
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    async with database.read_transaction() as reader:
        page = await AdmissionSweep().next_page(reader)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert len(page.node_ids) == 2
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.outstanding == database.units_in_flight() == 0
