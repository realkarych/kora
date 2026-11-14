import pytest

from kora.db.connection import Database
from kora.engine.readiness import ReadinessRejected, prepare_readiness
from kora.observability import Level, LoggingSettings
from tests.db.closing import ConnectionLedger
from tests.engine.test_graph_execution_contract import LIMITS
from tests.engine.test_readiness import PRIVATE, readiness_source
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
async def test_readiness_diagnostics_exclude_graph_material(
    configure: Configure, pipe: Pipe, database: Database, level: Level
) -> None:
    checked = await readiness_source(database)
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    prepared = prepare_readiness(
        checked, max_ir_bytes=LIMITS.resources.logical.ir_bytes
    )
    with pytest.raises(ReadinessRejected):
        prepare_readiness(checked, max_ir_bytes=1)
    finished_output(diagnostics, pipe)
    records = pipe.reader.records()

    assert [
        record["prepared"]
        for record in records
        if record["event"] == "engine.readiness.prepared"
    ] == ([True] if level is Level.DEBUG else [])
    for private in (
        PRIVATE,
        checked.scope.version_id,
        checked.pin.execution_contract_digest,
        "commit_sha",
        "none_failed_min_one_success",
    ):
        assert private.encode() not in pipe.reader.data
    assert prepared.matches(checked.scope, checked.pin)
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_readiness_preparation_survives_unavailable_sink(
    configure: Configure, pipe: Pipe, database: Database, broken: bool
) -> None:
    checked = await readiness_source(database)
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    first = prepare_readiness(checked, max_ir_bytes=LIMITS.resources.logical.ir_bytes)
    second = prepare_readiness(checked, max_ir_bytes=LIMITS.resources.logical.ir_bytes)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert first == second
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.checkouts == ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
