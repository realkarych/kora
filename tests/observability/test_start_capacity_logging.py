from dataclasses import replace

import pytest

from kora.db.connection import Database
from kora.engine.start_capacity import CapacityOutcome, InvalidCapacityPolicy, Overlap
from kora.observability import Level, LoggingSettings
from kora.protocols.authentication import Principal
from tests.db.seed import Seeder
from tests.engine.capacity_support import PRIVATE, capacity_scenario
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
@pytest.mark.parametrize("outcome", ["room", "full", "skipped", "refused", "invalid"])
async def test_start_capacity_diagnostics_are_content_free(
    database: Database,
    configure: Configure,
    pipe: Pipe,
    capsys: pytest.CaptureFixture[str],
    level: Level,
    outcome: str,
) -> None:
    scenario = await capacity_scenario(database)
    overlap: Overlap = "allow"
    if outcome in {"full", "skipped"}:
        async with database.transaction() as connection:
            await Seeder(connection).runs(
                scenario.namespace_id, scenario.graph_id, scenario.version_id, 2
            )
        if outcome == "skipped":
            overlap = "skip"
    elif outcome == "refused":
        scenario = replace(
            scenario,
            principal=Principal.model_validate(
                scenario.principal.model_dump() | {"namespaces": ()}
            ),
        )
    elif outcome == "invalid":
        await scenario.policy({"concurrency": {"max_pending_runs": PRIVATE}})
    before = await scenario.snapshot()
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    if outcome == "invalid":
        with pytest.raises(InvalidCapacityPolicy):
            await scenario.read(overlap)
    else:
        assert (await scenario.read(overlap)).outcome.value == outcome
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "engine.start_capacity.checked"
    ]
    assert len(records) == (1 if level is Level.DEBUG and outcome != "invalid" else 0)
    assert all(
        record["outcome"] == outcome
        and record["message"] == "manual start capacity checked"
        for record in records
    )
    for private in (
        PRIVATE,
        scenario.principal.token_id,
        scenario.namespace_id,
        scenario.graph_id,
        scenario.version_id,
        scenario.run_id,
    ):
        assert private not in output
    assert capsys.readouterr().out == ""
    assert before == await scenario.snapshot()
    assert database.writer_is_free()
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_start_capacity_survives_unavailable_sink(
    database: Database, configure: Configure, pipe: Pipe, broken: bool
) -> None:
    scenario = await capacity_scenario(database)
    before = await scenario.snapshot()
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    first = await scenario.read()
    second = await scenario.read()
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert first == second
    assert first.outcome is CapacityOutcome.ROOM
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert before == await scenario.snapshot()
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
