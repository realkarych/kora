from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from starlette.types import ASGIApp

from kora.api.exchange import ApiExchange
from kora.api.worker_history_route import WorkerHistoryRoute
from kora.cel_eval.thread import Evaluator
from kora.conformance.capture import captured
from kora.db.connection import Database
from kora.models.worker_claims import WorkerClaim
from kora.models.worker_history import WorkerHistoryQuery
from kora.observability import Level, LoggingSettings
from tests.api.claim_support import application
from tests.api.support import HttpCall
from tests.client.support import running_app
from tests.engine.claim_support import (
    HISTORY_LIMITS,
    WORKER_BYTES,
    ClaimCase,
    claim_case,
)
from tests.plugins.authentication.support import AuthorityClock


@pytest_asyncio.fixture
async def history_api(
    database: Database, evaluator: Evaluator, request: pytest.FixtureRequest
) -> AsyncGenerator[tuple[ClaimCase, AuthorityClock, ApiExchange]]:
    case = await claim_case(database, evaluator)
    clock = AuthorityClock()
    read_timeout: object = getattr(request, "param", 10.0)
    assert type(read_timeout) is float
    with captured(
        "api", LoggingSettings(level=Level.DEBUG), "history-api", clock.now
    ) as capture:
        route = WorkerHistoryRoute(
            database,
            HISTORY_LIMITS,
            clock,
            worker_bytes=WORKER_BYTES,
            read_timeout=read_timeout,
        )
        app = await application(database, capture, clock, case, route)
        yield case, clock, app


def history_query(granted: WorkerClaim) -> WorkerHistoryQuery:
    return WorkerHistoryQuery(
        lease_epoch=granted.lease_epoch,
        dispatcher_id=granted.current_grant.dispatcher_id,
        snapshot_version=1,
        snapshot_upper_epoch=granted.grant_history.upper_epoch,
        snapshot_count=granted.grant_history.count,
        snapshot_digest=granted.grant_history.digest,
        received_count=0,
        prefix_digest=granted.grant_history.seed(),
    )


async def raw_history(
    app: ASGIApp,
    path: str,
    query: bytes,
    headers: list[tuple[bytes, bytes]],
    transport: str,
) -> tuple[int, bytes]:
    if transport == "asgi":
        call = HttpCall(path=path, query=query, headers=headers)
        await call.run(app)
        return call.status, call.body
    async with running_app(app) as origin:
        async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
            response = await client.get(
                httpx.URL(origin + path).copy_with(query=query), headers=headers
            )
    return response.status_code, response.content
