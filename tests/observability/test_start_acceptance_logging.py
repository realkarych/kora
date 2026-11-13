import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.start_acceptance import AcceptanceOutcome, StartAcceptance
from kora.observability import Level, LoggingSettings
from tests.api.deployment_cases import OWNER, evaluator
from tests.engine.start_cases import KEY, deploy, request_for, start, start_rows
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)

__all__ = ["evaluator"]

EVENT = "engine.start_acceptance.completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
async def test_manual_start_acceptance_diagnostics_are_content_free(
    database: Database,
    evaluator: Evaluator,
    configure: Configure,
    pipe: Pipe,
    capsys: pytest.CaptureFixture[str],
    level: Level,
) -> None:
    case = await deploy(database, evaluator)
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    first = await start(database, case)
    replayed = await start(database, case, seconds=1)
    mismatched = await start(database, case, allow_unenforced_secrets=True, seconds=2)
    output = finished_output(diagnostics, pipe)
    rows = await start_rows(database)

    assert isinstance(first, StartAcceptance)
    assert isinstance(replayed, StartAcceptance)
    assert isinstance(mismatched, StartAcceptance)
    outcomes = [
        record["outcome"]
        for record in pipe.reader.records()
        if record["event"] == EVENT
    ]
    expected = [first.outcome, replayed.outcome, mismatched.outcome]
    assert expected == [
        AcceptanceOutcome.ACCEPTED,
        AcceptanceOutcome.REPLAYED,
        AcceptanceOutcome.MISMATCHED,
    ]
    assert outcomes == (
        [value.value for value in expected] if level is Level.DEBUG else []
    )
    [saved] = rows["run"]
    [receipt] = rows["receipt"]
    for private in (
        KEY,
        OWNER,
        case.user.token_id,
        case.namespace_id,
        case.graph_id,
        case.version_id,
        str(saved["id"]),
        str(receipt["id"]),
        request_for(case).identity.request_digest,
    ):
        assert private not in output
    assert capsys.readouterr().out == ""
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_manual_start_acceptance_survives_unavailable_sink(
    database: Database,
    evaluator: Evaluator,
    configure: Configure,
    pipe: Pipe,
    broken: bool,
) -> None:
    case = await deploy(database, evaluator)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    result = await start(database, case)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()
    rows = await start_rows(database)

    assert isinstance(result, StartAcceptance)
    assert result.outcome is AcceptanceOutcome.ACCEPTED
    assert (len(rows["run"]), len(rows["receipt"]), len(rows["event"])) == (1, 1, 1)
    assert snapshot.dropped_total() > 0
    assert drained
    assert not diagnostics.writer_is_alive()
    assert database.units_in_flight() == 0
