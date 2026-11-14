import pytest
from sqlalchemy import func, select

from kora.db.connection import Database
from kora.db.tables import price_table
from kora.engine.admission_initialization import AdmissionInitializationRefused
from kora.engine.namespace_policy import NamespacePolicyConfiguration
from kora.engine.price_initialization import PriceConfiguration
from kora.observability import Level, LoggingSettings
from tests.db.seed import START
from tests.engine.test_admission_initialization import initializer, namespace_id, rates
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)

PRIVATE: str = "private-admission-configuration"


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
@pytest.mark.parametrize("outcome", ["committed", "replayed", "refused"])
async def test_admission_initialization_diagnostics_exclude_configuration(
    database: Database,
    configure: Configure,
    pipe: Pipe,
    capsys: pytest.CaptureFixture[str],
    level: Level,
    outcome: str,
) -> None:
    identity = await namespace_id(database)
    configuration = NamespacePolicyConfiguration.prepare(
        identity, 2, {"private": PRIVATE}
    )
    service = initializer(
        database,
        (configuration,),
        (PriceConfiguration(None, START, {PRIVATE: rates()}),),
    )
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))
    if outcome != "committed":
        await service.initialize(diagnostics)

    if outcome == "refused":
        conflicting = initializer(
            database,
            (
                NamespacePolicyConfiguration.prepare(
                    identity, 2, {"private": "different"}
                ),
            ),
        )
        with pytest.raises(AdmissionInitializationRefused):
            await conflicting.initialize(diagnostics)
    else:
        await service.initialize(diagnostics)
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if str(record["event"]).startswith("engine.admission_initialization.")
    ]
    assert len(records) == (1 if outcome == "committed" else 2)
    assert records[-1]["event"] == (
        "engine.admission_initialization.refused"
        if outcome == "refused"
        else "engine.admission_initialization.finished"
    )
    if outcome != "refused":
        assert records[-1]["outcome"] == "committed"
    assert PRIVATE not in output
    assert identity not in output
    assert service.baseline.id not in output
    assert service.prices[0].id not in output
    assert capsys.readouterr().out == ""
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_admission_initialization_commits_with_unavailable_sink(
    database: Database, configure: Configure, pipe: Pipe, broken: bool
) -> None:
    service = initializer(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    await service.initialize(diagnostics)
    await service.initialize(diagnostics)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    async with database.read_transaction() as connection:
        assert (
            await connection.scalar(select(func.count()).select_from(price_table)) == 1
        )
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
