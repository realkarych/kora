import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import update

from kora.api.worker_history_route import WorkerHistoryRoute
from kora.cel_eval.thread import Evaluator
from kora.client import KoraClient
from kora.client.errors import ApiError
from kora.conformance.capture import captured
from kora.db.connection import Database, DatabaseSettings
from kora.db.tables import attempt_dispatcher
from kora.observability import Level, LoggingSettings
from tests.api.claim_support import BEARER, application, request
from tests.api.deployment_cases import evaluator
from tests.api.history_sink_probe import (
    HistoryProbePhase,
    last_progress,
    record_progress,
)
from tests.api.history_support import history_query
from tests.api.sink_probe import SinkReport
from tests.api.test_history_lifetime import HeldHistory
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import HISTORY_LIMITS, WORKER_BYTES, claim_case
from tests.plugins.authentication.support import AuthorityClock
from tests.support.observability import run_python

__all__ = ["evaluator"]


@pytest.mark.parametrize("sink", ["blocked", "broken"])
def test_history_logging_sink_failure_preserves_history_and_claim_progress(
    database_settings: DatabaseSettings, tmp_path: Path, sink: str
) -> None:
    configuration = tmp_path / "history-database.json"
    configuration.write_text(
        json.dumps(database_settings.url.render_as_string(hide_password=False))
    )
    progress = tmp_path / "history-progress.txt"
    record_progress(progress, HistoryProbePhase.IMPORT, 0)

    try:
        process = run_python(
            [
                "-m",
                "tests.api.history_sink_probe",
                sink,
                str(configuration),
                str(progress),
            ],
            {},
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError(
            f"history probe timed out; last entered phase: {last_progress(progress)}"
        ) from error

    assert process.code == 0, process.stderr.decode()
    report = SinkReport.model_validate_json(process.stdout)
    assert report.completed == 96
    assert report.outstanding == 0
    assert not report.writer_alive
    assert process.stderr == b""
    assert b"private-" not in process.stdout
    if sink == "blocked":
        assert report.dropped > 0
    else:
        assert report.failed


@pytest.mark.asyncio
async def test_history_diagnostics_exclude_query_process_and_native_error_content(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    clock = AuthorityClock()
    ledger = ConnectionLedger(database)
    private = "private-history-process-content"
    with captured(
        "api", LoggingSettings(level=Level.DEBUG), "history-content", clock.now
    ) as capture:
        route = WorkerHistoryRoute(
            database, HISTORY_LIMITS, clock, worker_bytes=WORKER_BYTES, read_timeout=10
        )
        app = await application(database, capture, clock, case, route)
        async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
            granted = (
                await client.claim_worker(case.query.worker_id, request(case))
            ).claim
            assert granted is not None
            query = history_query(granted)
            first = await client.grant_history(case.binding.attempt_id, query)
            replay = await client.grant_history(case.binding.attempt_id, query)
            with pytest.raises(ApiError) as conflict:
                await client.grant_history(
                    case.binding.attempt_id,
                    query.model_copy(update={"snapshot_digest": "f" * 64}),
                )
            held = HeldHistory(app)
            async with KoraClient(
                "http://127.0.0.1", BEARER, asgi_app=held
            ) as interrupted:
                pending = asyncio.create_task(
                    interrupted.grant_history(case.binding.attempt_id, query)
                )
                try:
                    await asyncio.wait_for(held.reached.wait(), 10)
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                finally:
                    held.release.set()
                    await asyncio.gather(pending, return_exceptions=True)
            async with database.transaction() as writer:
                await writer.execute(
                    update(attempt_dispatcher).values(process={"private": private})
                )
            with pytest.raises(ApiError) as corrupted:
                await client.grant_history(case.binding.attempt_id, query)
        records = capture.finish()

    history = [
        record for record in records if record["event"] == "engine.worker_history.read"
    ]
    rendered = json.dumps(records)
    assert first == replay
    assert conflict.value.code == "API002"
    assert corrupted.value.code == "API010"
    assert len(history) == 3
    assert all(
        record["rows"] == 1 and type(record["bytes"]) is int for record in history
    )
    assert BEARER not in rendered
    assert private not in rendered
    assert query.prefix_digest not in rendered
    assert query.snapshot_digest not in rendered
    assert "ValidationError" not in rendered
    assert ledger.outstanding == database.units_in_flight() == 0
