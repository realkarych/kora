import pytest
from sqlalchemy import select

from kora.db.connection import Database
from kora.db.tables import wait
from kora.engine.attempt_admission import AttemptAdmissionState, admit_attempt
from kora.engine.attempt_capacity import PreparedGraphCaps
from kora.observability import Level, LoggingSettings
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.admission_wait_support import INTERVAL, LIMITS
from tests.engine.attempt_admission_support import (
    append_admission_case,
    attempt_admission_case,
)
from tests.engine.expiry_support import expiry_context
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
async def test_attempt_admission_diagnostics_exclude_sources_and_describe_preparation(
    configure: Configure, pipe: Pipe, database: Database, level: Level
) -> None:
    case = await attempt_admission_case(database)
    expiring = await append_admission_case(database, case)
    prepared, expiry_prepared = case.prepare(), expiring.prepare()
    caps = PreparedGraphCaps(case.promotion.scope, None, None)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        await seed.attempt(await seed.node(case.promotion.run_id, "private"), "created")
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    states: list[AttemptAdmissionState] = []
    expected: str | None = None
    for limit in (1, 2, 2):
        if expected is not None:
            case.promotion.clock.advance(INTERVAL.total_seconds())
        async with database.transaction() as writer:
            states.append(
                await admit_attempt(
                    await expiry_context(writer, case.promotion),
                    prepared,
                    LIMITS,
                    caps,
                    global_limit=limit,
                    runtime_limit=None,
                    interval=INTERVAL,
                    expected_wait_id=expected,
                    periodic_recheck=True,
                )
            )
        async with database.read_transaction() as reader:
            expected = await reader.scalar(select(wait.c.id))
    expiring.promotion.clock.advance(3600)
    async with database.transaction() as writer:
        states.append(
            await admit_attempt(
                await expiry_context(writer, expiring.promotion),
                expiry_prepared,
                LIMITS,
                caps,
                global_limit=3,
                runtime_limit=None,
                interval=INTERVAL,
            )
        )
    finished_output(diagnostics, pipe)
    records = pipe.reader.records()

    assert states == [
        AttemptAdmissionState.WAITING,
        AttemptAdmissionState.QUEUED,
        AttemptAdmissionState.REFUSED,
        AttemptAdmissionState.EXPIRED,
    ]
    assert [
        record["outcome"]
        for record in records
        if record["event"] == "engine.attempt_admission.prepared"
    ] == ([state.value for state in states] if level is Level.DEBUG else [])
    for private in (
        case.promotion.run_id,
        case.binding.node_id,
        case.binding.node_path,
        case.binding.attempt_id,
        case.price.id,
        case.checked.pin.execution_contract_digest,
        "private",
        "input_bindings",
    ):
        assert private.encode() not in pipe.reader.data
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_attempt_admission_releases_writer_with_unavailable_sink(
    configure: Configure, pipe: Pipe, database: Database, broken: bool
) -> None:
    case = await attempt_admission_case(database)
    prepared = case.prepare()
    caps = PreparedGraphCaps(case.promotion.scope, None, None)
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    async with database.transaction() as writer:
        result = await admit_attempt(
            await expiry_context(writer, case.promotion),
            prepared,
            LIMITS,
            caps,
            global_limit=1,
            runtime_limit=1,
            interval=INTERVAL,
        )
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert result is AttemptAdmissionState.QUEUED
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
