import pytest
from sqlalchemy import update

from kora.db.attempt_capacity import RunRecovery
from kora.db.connection import Database
from kora.db.tables import attempt
from kora.engine.attempt_capacity import (
    AttemptCapacityRefused,
    CapacityLimit,
    check_attempt_capacity,
)
from kora.observability import Level, LoggingSettings
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.attempt_capacity_support import (
    PRIVATE,
    capacity_case,
    namespace_limit,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
async def test_attempt_capacity_diagnostics_exclude_configuration(
    configure: Configure, pipe: Pipe, database: Database, level: Level
) -> None:
    case = await capacity_case(database)
    await namespace_limit(database, case.scope.namespace_id, 10)
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    async with database.transaction() as writer:
        context = await case.context(writer)
        room = await check_attempt_capacity(
            context, "local", case.caps, global_limit=1, runtime_limit=None
        )
        seed = Seeder(writer)
        attempt_id = await seed.attempt(
            await seed.node(case.run_id, "private-node"), "created"
        )
        full = await check_attempt_capacity(
            context, "local", case.caps, global_limit=1, runtime_limit=None
        )
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == attempt_id)
            .values(recovery_required=True)
        )
        recovery = await check_attempt_capacity(
            context, "local", case.caps, global_limit=1, runtime_limit=None
        )
        with pytest.raises(AttemptCapacityRefused) as caught:
            await check_attempt_capacity(
                context, "local", case.caps, global_limit=0, runtime_limit=None
            )
    finished_output(diagnostics, pipe)
    records = pipe.reader.records()

    assert room is None
    assert isinstance(full, CapacityLimit)
    assert recovery == RunRecovery(1, (attempt_id,))
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert [
        record["outcome"]
        for record in records
        if record["event"] == "engine.attempt_capacity.checked"
    ] == (
        ["room", "concurrency_limit", "execution_recovery"]
        if level is Level.DEBUG
        else []
    )
    for private in (
        PRIVATE,
        "private-node",
        case.run_id,
        case.scope.version_id,
        attempt_id,
    ):
        assert private.encode() not in pipe.reader.data
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_attempt_capacity_releases_writer_with_unavailable_sink(
    configure: Configure, pipe: Pipe, database: Database, broken: bool
) -> None:
    case = await capacity_case(database)
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    async with database.transaction() as writer:
        context = await case.context(writer)
        first = await check_attempt_capacity(
            context, "local", case.caps, global_limit=1, runtime_limit=1
        )
        second = await check_attempt_capacity(
            context, "local", case.caps, global_limit=1, runtime_limit=1
        )
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert first is second is None
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
