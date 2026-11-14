import asyncio
import hashlib
import json
import os
import sys
from enum import StrEnum
from pathlib import Path

import pytest
from sqlalchemy import update

from kora.api.application import create_application
from kora.api.exchange import COMPONENT
from kora.api.worker_claims_route import WorkerClaimsRoute
from kora.api.worker_history_route import WorkerHistoryRoute
from kora.cel_eval.binding import load_binding
from kora.cel_eval.thread import EvaluationThread, Evaluator
from kora.client import KoraClient
from kora.client.errors import ApiError
from kora.db.connection import Database, parse_database_url
from kora.db.tables import api_token
from kora.observability import (
    Level,
    LoggingSettings,
    ProcessDiagnostics,
    configure_process,
)
from kora.observability.sink import DiagnosticsSnapshot
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from tests.api.claim_support import BEARER, poller, request
from tests.api.history_support import history_query
from tests.api.sink_probe import SinkReport
from tests.api.support import RequestIds
from tests.db.closing import ConnectionLedger, reached
from tests.engine.claim_support import HISTORY_LIMITS, WORKER_BYTES, claim_case
from tests.plugins.authentication.support import AuthorityClock
from tests.plugins.storage.sink_probe import fill


class HistoryProbePhase(StrEnum):
    IMPORT = "import"
    SETUP = "setup"
    INITIAL_CLAIM = "initial_claim"
    HISTORY_READ = "history_read"
    CONFLICT_READ = "conflict_read"
    CLAIM_REPLAY = "claim_replay"
    CLIENT_CLOSE = "client_close"
    EVALUATOR_CLOSE = "evaluator_close"
    DATABASE_CLOSE = "database_close"
    DIAGNOSTICS_CLOSE = "diagnostics_close"
    COMPLETE = "complete"


def record_progress(path: Path, phase: HistoryProbePhase, completed: int) -> None:
    pending = path.with_suffix(".next")
    pending.write_text(f"{phase.value} {completed}\n", encoding="ascii")
    pending.replace(path)


def last_progress(path: Path) -> str:
    try:
        with path.open("rb") as source:
            encoded = source.read(128)
        fields = encoded.decode("ascii").split()
        if len(encoded) == 128 or len(fields) != 2:
            return "unknown"
        phase = HistoryProbePhase(fields[0])
        completed = int(fields[1])
    except (OSError, ValueError):
        return "unknown"
    if not 0 <= completed <= 96:
        return "unknown"
    return f"{phase.value}; completed={completed}"


def run(sink: str, configuration: Path, progress: Path) -> SinkReport:
    record_progress(progress, HistoryProbePhase.SETUP, 0)
    data: object = json.loads(configuration.read_bytes())
    assert isinstance(data, str)
    read_end, write_end = os.pipe()
    if sink == "blocked":
        fill(write_end)
    else:
        os.close(read_end)
    clock = AuthorityClock()
    database = Database(parse_database_url(data))
    ledger = ConnectionLedger(database)
    completed = 0
    diagnostics: ProcessDiagnostics | None = None
    snapshot: DiagnosticsSnapshot | None = None
    drained = False

    async def exercise() -> None:
        nonlocal completed, diagnostics
        evaluator = Evaluator(clock.now, EvaluationThread(load_binding()))
        try:
            case = await claim_case(database, evaluator)
            diagnostics = configure_process(
                COMPONENT,
                LoggingSettings(level=Level.DEBUG, queue_records=16, queue_bytes=65536),
                "history-sink",
                clock.now,
                write_end,
            )
            async with database.transaction() as writer:
                await writer.execute(
                    update(api_token)
                    .where(api_token.c.id == case.principal.token_id)
                    .values(
                        digest=hashlib.sha256(BEARER.encode()).hexdigest(),
                        kind="service",
                    )
                )
            app = create_application(
                StaticTokenAuthenticator({}, database),
                diagnostics,
                clock,
                RequestIds(),
                worker_claims=WorkerClaimsRoute(poller(database, clock)),
                worker_history=WorkerHistoryRoute(
                    database,
                    HISTORY_LIMITS,
                    clock,
                    worker_bytes=WORKER_BYTES,
                    read_timeout=10,
                ),
            )
            async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
                record_progress(progress, HistoryProbePhase.INITIAL_CLAIM, completed)
                first = await client.claim_worker(case.query.worker_id, request(case))
                assert first.claim is not None
                query = history_query(first.claim)
                conflict = query.model_copy(update={"snapshot_digest": "f" * 64})
                for _ in range(32):
                    record_progress(progress, HistoryProbePhase.HISTORY_READ, completed)
                    page = await client.grant_history(case.binding.attempt_id, query)
                    assert page.complete
                    completed += 1
                    record_progress(
                        progress, HistoryProbePhase.CONFLICT_READ, completed
                    )
                    with pytest.raises(ApiError) as caught:
                        await client.grant_history(case.binding.attempt_id, conflict)
                    assert caught.value.code == "API002"
                    completed += 1
                    record_progress(progress, HistoryProbePhase.CLAIM_REPLAY, completed)
                    replay = await client.claim_worker(
                        case.query.worker_id, request(case)
                    )
                    assert replay == first
                    completed += 1
                record_progress(progress, HistoryProbePhase.CLIENT_CLOSE, completed)
        finally:
            try:
                try:
                    record_progress(
                        progress, HistoryProbePhase.EVALUATOR_CLOSE, completed
                    )
                finally:
                    await evaluator.close()
            finally:
                try:
                    record_progress(
                        progress, HistoryProbePhase.DATABASE_CLOSE, completed
                    )
                finally:
                    await database.close()

    try:
        asyncio.run(exercise())
    finally:
        try:
            if diagnostics is not None:
                try:
                    record_progress(
                        progress, HistoryProbePhase.DIAGNOSTICS_CLOSE, completed
                    )
                finally:
                    snapshot = diagnostics.snapshot()
                    drained = diagnostics.shutdown(1)
        finally:
            if sink == "blocked":
                os.close(read_end)
            try:
                if diagnostics is not None:
                    closing = diagnostics
                    assert asyncio.run(
                        reached(lambda: not closing.writer_is_alive(), 1)
                    )
            finally:
                os.close(write_end)
    assert snapshot is not None
    assert diagnostics is not None
    result = SinkReport(
        completed=completed,
        outstanding=ledger.outstanding,
        writer_alive=diagnostics.writer_is_alive(),
        drained=drained,
        dropped=snapshot.dropped_total(),
        failed=snapshot.sink_failed,
    )
    record_progress(progress, HistoryProbePhase.COMPLETE, completed)
    return result


if __name__ == "__main__":
    sys.stdout.write(
        run(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])).model_dump_json() + "\n"
    )
