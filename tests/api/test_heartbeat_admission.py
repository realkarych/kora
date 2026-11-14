from dataclasses import replace

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection

from kora.api.worker_heartbeat_route import WorkerHeartbeatRoute
from kora.cel_eval.thread import Evaluator
from kora.client import KoraClient
from kora.client.errors import ApiError
from kora.conformance.capture import captured
from kora.db.connection import Database
from kora.observability import Level, LoggingSettings
from kora.protocols.worker_heartbeats import WorkerHeartbeat
from tests.api.claim_support import BEARER, application, request
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import WORKER_BYTES, claim_case, state
from tests.engine.heartbeat_support import HEARTBEAT_LIMITS
from tests.engine.promotion_support import READ_LIMITS
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["native", "raw"])
async def test_heartbeat_route_admits_aggregate_before_worker_locking_read(
    database: Database, evaluator: Evaluator, budget: str
) -> None:
    case = await claim_case(database, evaluator)
    clock = AuthorityClock()
    parent_bytes = (
        HEARTBEAT_LIMITS.native_bytes + 1
        if budget == "native"
        else HEARTBEAT_LIMITS.raw_bytes + 1
    )
    statements: list[str] = []

    def before_statement(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    with captured(
        "api", LoggingSettings(level=Level.DEBUG), "heartbeat-admission", clock.now
    ) as capture:
        route = WorkerHeartbeatRoute(
            database,
            HEARTBEAT_LIMITS,
            replace(READ_LIMITS, parent_bytes=parent_bytes),
            clock,
            case.identifiers,
            worker_bytes=WORKER_BYTES,
            read_timeout=10,
            lock_timeout_ms=1000,
        )
        app = await application(database, capture, clock, case, heartbeat=route)
        async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
            claim = (
                await client.claim_worker(case.query.worker_id, request(case))
            ).claim
            assert claim is not None
            before = await state(database)
            engine = database.writer_engine.sync_engine
            event.listen(engine, "before_cursor_execute", before_statement)

            try:
                with pytest.raises(ApiError) as caught:
                    await client.heartbeat_attempt(
                        claim.attempt_id,
                        WorkerHeartbeat(
                            dispatcher_id=claim.current_grant.dispatcher_id,
                            lease_epoch=claim.lease_epoch,
                        ),
                    )
            finally:
                event.remove(engine, "before_cursor_execute", before_statement)
            after = await state(database)

    assert caught.value.code == "API010"
    assert not any("FROM worker" in statement for statement in statements)
    assert before == after
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
