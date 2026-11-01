from dataclasses import replace

import pytest
from sqlalchemy import update

from kora.db.connection import Database
from kora.db.tables import inbound_event
from kora.engine.start_records import StartRecordRejected, prepare_manual_start
from kora.engine.start_replay import ReplayOutcome
from kora.observability import Level, LoggingSettings
from kora.protocols.authentication import Principal
from tests.engine.start_support import KEY, PRIVATE, replay_scenario
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
@pytest.mark.parametrize(
    "outcome", ["matched", "absent", "mismatched", "refused", "invalid"]
)
async def test_manual_start_replay_diagnostics_are_content_free(
    database: Database,
    configure: Configure,
    pipe: Pipe,
    capsys: pytest.CaptureFixture[str],
    level: Level,
    outcome: str,
) -> None:
    scenario = await replay_scenario(database)
    if outcome == "absent":
        scenario = replace(
            scenario,
            identity=prepare_manual_start(
                scenario.identity.namespace_id, scenario.version_id, {}
            ),
        )
    elif outcome == "mismatched":
        scenario = replace(
            scenario,
            identity=prepare_manual_start(
                scenario.identity.namespace_id, scenario.identity.graph_id, {}
            ),
        )
    elif outcome == "refused":
        scenario = replace(
            scenario,
            principal=Principal.model_validate(
                scenario.principal.model_dump() | {"namespaces": ()}
            ),
        )
    elif outcome == "invalid":
        async with database.transaction() as connection:
            await connection.execute(
                update(inbound_event).values(start_outcome={"private": PRIVATE})
            )
    before = await scenario.snapshot()
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    if outcome == "invalid":
        with pytest.raises(StartRecordRejected):
            await scenario.read()
    else:
        assert (await scenario.read()).outcome.value == outcome
    output = finished_output(diagnostics, pipe)
    after = await scenario.snapshot()

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "engine.start_replay.checked"
    ]
    assert len(records) == (1 if level is Level.DEBUG and outcome != "invalid" else 0)
    assert all(
        record["outcome"] == outcome
        and record["message"] == "manual start replay checked"
        for record in records
    )
    for private in (
        PRIVATE,
        KEY,
        scenario.identity.request_digest,
        scenario.receipt.request_digest,
        scenario.principal.owner,
        scenario.principal.token_id,
        scenario.identity.namespace_id,
        scenario.identity.graph_id,
        scenario.version_id,
    ):
        assert private not in output
    assert capsys.readouterr().out == ""
    assert before == after
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_manual_start_replay_survives_unavailable_sink(
    database: Database, configure: Configure, pipe: Pipe, broken: bool
) -> None:
    scenario = await replay_scenario(database)
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
    after = await scenario.snapshot()

    assert first == second
    assert first.outcome is ReplayOutcome.MATCHED
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert before == after
    assert database.units_in_flight() == 0
