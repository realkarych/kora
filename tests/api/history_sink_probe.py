import asyncio
import hashlib
import json
import os
import sys
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
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from tests.api.claim_support import BEARER, poller, request
from tests.api.history_support import history_query
from tests.api.sink_probe import SinkReport
from tests.api.support import RequestIds
from tests.db.closing import ConnectionLedger, reached
from tests.engine.claim_support import HISTORY_LIMITS, WORKER_BYTES, claim_case
from tests.plugins.authentication.support import AuthorityClock
from tests.plugins.storage.sink_probe import fill


def run(sink: str, configuration: Path) -> SinkReport:
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

    async def exercise() -> ProcessDiagnostics:
        nonlocal completed
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
                first = await client.claim_worker(case.query.worker_id, request(case))
                assert first.claim is not None
                query = history_query(first.claim)
                conflict = query.model_copy(update={"snapshot_digest": "f" * 64})
                for _ in range(32):
                    page = await client.grant_history(case.binding.attempt_id, query)
                    assert page.complete
                    with pytest.raises(ApiError) as caught:
                        await client.grant_history(case.binding.attempt_id, conflict)
                    assert caught.value.code == "API002"
                    replay = await client.claim_worker(
                        case.query.worker_id, request(case)
                    )
                    assert replay == first
                    completed += 3
            return diagnostics
        finally:
            await evaluator.close()
            await database.close()

    diagnostics = asyncio.run(exercise())
    snapshot = diagnostics.snapshot()
    drained = diagnostics.shutdown(1)
    if sink == "blocked":
        os.close(read_end)
    assert asyncio.run(reached(lambda: not diagnostics.writer_is_alive(), 1))
    os.close(write_end)
    return SinkReport(
        completed=completed,
        outstanding=ledger.outstanding,
        writer_alive=diagnostics.writer_is_alive(),
        drained=drained,
        dropped=snapshot.dropped_total(),
        failed=snapshot.sink_failed,
    )


if __name__ == "__main__":
    sys.stdout.write(run(sys.argv[1], Path(sys.argv[2])).model_dump_json() + "\n")
