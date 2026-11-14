import traceback

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.db.connection import Database
from kora.engine.admission_processing import AdmissionProcessingFailed
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.observability import Level, LoggingSettings
from tests.api.deployment_cases import preparer
from tests.db.closing import ConnectionLedger
from tests.engine.admission_processing_support import process, processor
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)
from tests.observability.test_admission_snapshot_diagnostics import seed_snapshot

__all__ = ["preparer"]
PRIVATE = "private-admission-candidate"


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
async def test_admission_processing_diagnostics_follow_settlement(
    database: Database,
    preparer: DeploymentPreparer,
    configure: Configure,
    pipe: Pipe,
    level: Level,
) -> None:
    case = await seed_snapshot(database, preparer)
    owner = processor(database, case)
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    outcome = await process(owner, case.node_id)
    with pytest.raises(AdmissionProcessingFailed) as failed:
        await process(owner, PRIVATE)
    finished_output(diagnostics, pipe)

    assert outcome is AttemptAdmissionState.QUEUED
    assert [
        record["outcome"]
        for record in pipe.reader.records()
        if record["event"] == "engine.admission_processing.completed"
    ] == (["queued", "failed"] if level is Level.DEBUG else [])
    assert failed.value.__cause__ is failed.value.__context__ is None
    assert PRIVATE not in "".join(traceback.format_exception(failed.value))
    assert PRIVATE.encode() not in pipe.reader.data
    assert case.node_id.encode() not in pipe.reader.data
    assert case.promotion.run_id.encode() not in pipe.reader.data
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_admission_processing_releases_resources_with_unavailable_sink(
    database: Database,
    preparer: DeploymentPreparer,
    configure: Configure,
    pipe: Pipe,
    broken: bool,
) -> None:
    case = await seed_snapshot(database, preparer)
    owner = processor(database, case)
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    outcome = await process(owner, case.node_id)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert outcome is AttemptAdmissionState.QUEUED
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
