from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio

from kora.api.exchange import ApiExchange
from kora.api.graph_deployment import PreparedDeployment
from kora.api.worker_heartbeat_route import WorkerHeartbeatRoute
from kora.api.worker_history_route import WorkerHistoryRoute
from kora.cel_eval.thread import Evaluator
from kora.conformance.capture import captured
from kora.db.connection import Database
from kora.observability import Level, LoggingSettings
from tests.api.claim_support import application
from tests.engine.claim_support import (
    HISTORY_LIMITS,
    WORKER_BYTES,
    ClaimCase,
    claim_case,
    claim_case_from_deployment,
)
from tests.engine.heartbeat_support import HEARTBEAT_LIMITS
from tests.engine.promotion_support import READ_LIMITS
from tests.plugins.authentication.support import AuthorityClock


@pytest_asyncio.fixture
async def heartbeat_api(
    database: Database, evaluator: Evaluator, request: pytest.FixtureRequest
) -> AsyncGenerator[tuple[ClaimCase, AuthorityClock, ApiExchange]]:
    case = await claim_case(database, evaluator)
    async with _heartbeat_api(database, case, request) as fixture:
        yield fixture


@pytest_asyncio.fixture
async def heartbeat_api_from_deployment(
    database: Database,
    default_prepared_deployment: PreparedDeployment,
    request: pytest.FixtureRequest,
) -> AsyncGenerator[tuple[ClaimCase, AuthorityClock, ApiExchange]]:
    case = await claim_case_from_deployment(database, default_prepared_deployment)
    async with _heartbeat_api(database, case, request) as fixture:
        yield fixture


@asynccontextmanager
async def _heartbeat_api(
    database: Database, case: ClaimCase, request: pytest.FixtureRequest
) -> AsyncGenerator[tuple[ClaimCase, AuthorityClock, ApiExchange]]:
    clock = AuthorityClock()
    timeout: object = getattr(request, "param", 1000)
    assert type(timeout) is int
    with captured(
        "api", LoggingSettings(level=Level.DEBUG), "heartbeat-api", clock.now
    ) as capture:
        route = WorkerHeartbeatRoute(
            database,
            HEARTBEAT_LIMITS,
            READ_LIMITS,
            clock,
            case.identifiers,
            worker_bytes=WORKER_BYTES,
            read_timeout=10.0,
            lock_timeout_ms=timeout,
        )
        history = WorkerHistoryRoute(
            database,
            HISTORY_LIMITS,
            clock,
            worker_bytes=WORKER_BYTES,
            read_timeout=10.0,
        )
        app = await application(
            database, capture, clock, case, history, heartbeat=route
        )
        yield case, clock, app
