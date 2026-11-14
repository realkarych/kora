import asyncio
import traceback

import pytest
from sqlalchemy import update

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import captured
from kora.db.connection import Database
from kora.db.native_values import NAMESPACE_POLICY_NATIVE_LIMIT
from kora.db.tables import namespace
from kora.engine.admission_snapshot import (
    AdmissionSnapshotRejected,
    acquire_admission_snapshot,
)
from kora.engine.locks import COMPONENT
from kora.observability import Level, LoggingSettings
from tests.api.deployment_cases import preparer
from tests.db.closing import ConnectionLedger
from tests.engine.admission_snapshot_support import (
    LIMITS,
    AdmissionSnapshotCase,
    acquire,
    checked,
    snapshot_case,
)
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

__all__ = ["preparer"]
PRIVATE = "private-admission-snapshot-value"


async def seed_snapshot(
    database: Database, preparer: DeploymentPreparer
) -> AdmissionSnapshotCase:
    with captured(
        COMPONENT, LoggingSettings(), "admission-snapshot", AuthorityClock().now
    ) as capture:
        return await snapshot_case(database, preparer, capture)


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
async def test_admission_snapshot_diagnostics_exclude_rejected_sources(
    database: Database,
    preparer: DeploymentPreparer,
    configure: Configure,
    pipe: Pipe,
    level: Level,
) -> None:
    case = await seed_snapshot(database, preparer)
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    successful = await acquire(database, case)
    checked(successful.graph, case.promotion.pin, case.promotion.scope)
    async with database.transaction() as writer:
        await writer.execute(
            update(namespace)
            .where(namespace.c.id == case.promotion.scope.namespace_id)
            .values(policy={PRIVATE: "x" * NAMESPACE_POLICY_NATIVE_LIMIT})
        )
    with pytest.raises(AdmissionSnapshotRejected) as source:
        await acquire(database, case)
    with pytest.raises(AdmissionSnapshotRejected) as deadline:
        await acquire_admission_snapshot(
            database,
            case.node_id,
            LIMITS,
            observed_at=case.promotion.clock.now(),
            periodic_recheck=True,
            deadline=asyncio.get_running_loop().time() - 1.0,
        )
    finished_output(diagnostics, pipe)

    records = pipe.reader.records()
    assert [
        record["acquired"]
        for record in records
        if record["event"] == "engine.admission_snapshot.acquired"
    ] == ([True, False, False] if level is Level.DEBUG else [])
    for caught in (source.value, deadline.value):
        assert caught.__cause__ is caught.__context__ is None
        assert str(caught) == "admission sources are unavailable or invalid"
        assert PRIVATE not in "".join(traceback.format_exception(caught))
    assert PRIVATE.encode() not in pipe.reader.data
    assert case.node_id.encode() not in pipe.reader.data
    assert case.promotion.run_id.encode() not in pipe.reader.data
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_admission_snapshot_native_failure_is_content_free(
    database: Database,
    preparer: DeploymentPreparer,
    configure: Configure,
    pipe: Pipe,
) -> None:
    case = await seed_snapshot(database, preparer)
    async with database.transaction() as writer:
        await writer.exec_driver_sql("DROP TABLE price_table")
    ledger = ConnectionLedger(database)
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    with pytest.raises(AdmissionSnapshotRejected) as failed:
        await acquire(database, case)
    finished_output(diagnostics, pipe)

    assert failed.value.__cause__ is failed.value.__context__ is None
    assert "price_table" not in "".join(traceback.format_exception(failed.value))
    assert b"price_table" not in pipe.reader.data
    assert case.node_id.encode() not in pipe.reader.data
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_admission_snapshot_survives_unavailable_sink(
    database: Database,
    preparer: DeploymentPreparer,
    configure: Configure,
    pipe: Pipe,
    broken: bool,
) -> None:
    case = await seed_snapshot(database, preparer)
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    acquired = await acquire(database, case)
    result = checked(acquired.graph, case.promotion.pin, case.promotion.scope)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert result.scope == case.promotion.scope
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
