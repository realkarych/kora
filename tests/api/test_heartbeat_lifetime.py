import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Connection
from sqlalchemy.util import await_

from kora.api.exchange import ApiExchange
from kora.client import KoraClient
from kora.db.connection import Database, Lifecycle
from kora.db.tables import worker
from kora.engine.locks import TransitionLocks
from kora.protocols.evidence import ClosureEvidence
from kora.protocols.worker_heartbeats import WorkerHeartbeat
from tests.api.claim_support import BEARER, request
from tests.api.deployment_cases import evaluator
from tests.api.heartbeat_support import heartbeat_api
from tests.api.history_support import history_query
from tests.api.test_history_lifetime import HeldHistory, HistoryClient
from tests.api.test_usage_lifetime import UsageCall
from tests.client.support import BOUND, running_app
from tests.db.closing import ConnectionLedger, until
from tests.engine.claim_support import ClaimCase, state
from tests.engine.heartbeat_support import self_closure
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator", "heartbeat_api"]


async def call_for(app: ApiExchange, case: ClaimCase, pause: str = "none") -> UsageCall:
    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        granted = (await client.claim_worker(case.query.worker_id, request(case))).claim
    assert granted is not None
    body = WorkerHeartbeat(
        dispatcher_id=granted.current_grant.dispatcher_id,
        lease_epoch=granted.lease_epoch,
        grant_history_complete=granted.grant_history,
        evidence=ClosureEvidence(
            runtime=None, dispatchers=(self_closure(granted),), publication=None
        ),
    )
    return UsageCall(
        path=f"/api/v1/attempts/{granted.attempt_id}/heartbeat",
        method="POST",
        headers=[
            (b"authorization", ("Bearer " + BEARER).encode()),
            (b"content-type", b"application/json"),
        ],
        payload=body.model_dump_json().encode(),
        pause=pause,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
@pytest.mark.parametrize("capacity", [1, 2])
async def test_heartbeat_progress_survives_slow_history_reader(
    database: Database,
    heartbeat_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    transport: str,
    capacity: int,
) -> None:
    case, clock, app = heartbeat_api
    held = HeldHistory(app)
    ledger = ConnectionLedger(database)
    async with (
        running_app(held) as origin,
        HistoryClient(
            "http://127.0.0.1" if transport == "asgi" else origin,
            BEARER,
            max_connections=capacity,
            asgi_app=held if transport == "asgi" else None,
        ) as client,
    ):
        granted = (await client.claim_worker(case.query.worker_id, request(case))).claim
        assert granted is not None
        query = history_query(granted)
        reading = asyncio.create_task(client.grant_history(granted.attempt_id, query))
        try:
            await asyncio.wait_for(held.reached.wait(), BOUND)
            assert ledger.outstanding == database.units_in_flight() == 0
            clock.moment += timedelta(seconds=16)

            accepted = await asyncio.wait_for(
                client.heartbeat_attempt(
                    granted.attempt_id,
                    WorkerHeartbeat(
                        dispatcher_id=granted.current_grant.dispatcher_id,
                        lease_epoch=granted.lease_epoch,
                    ),
                ),
                BOUND,
            )
            saved = await state(database)

            assert accepted.lease_until == clock.now() + timedelta(seconds=120)
            assert saved["attempt"][0]["lease_until"] == accepted.lease_until
            assert ledger.outstanding == database.units_in_flight() == 0
            if capacity == 1:
                with pytest.raises(asyncio.CancelledError):
                    await reading
            else:
                assert not reading.done()
                held.release.set()
                assert (await asyncio.wait_for(reading, BOUND)).complete
        finally:
            held.release.set()
            if not reading.done():
                reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)
        assert (await client.grant_history(granted.attempt_id, query)).complete

        assert client.admission_state() == (0, 0)
        assert client.request_count() == 0
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", ["body", "response"])
async def test_heartbeat_cancellation_and_lost_response_release_database(
    database: Database,
    heartbeat_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    pause: str,
) -> None:
    case, _, app = heartbeat_api
    call = await call_for(app, case, pause)
    ledger = ConnectionLedger(database)
    before = await state(database)
    pending = asyncio.create_task(call.run(app))

    try:
        await asyncio.wait_for(call.reached.wait(), BOUND)
        assert ledger.outstanding == database.units_in_flight() == 0
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, BOUND)
    finally:
        call.release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    after = await state(database)
    call.pause = "none"
    await call.run(app)
    repeated = await state(database)

    assert (after == before) is (pause == "body")
    assert call.status == 200
    if pause == "response":
        assert repeated == after
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeat_api", [100], indirect=True)
@pytest.mark.parametrize("held", ["run", "worker"])
async def test_heartbeat_lock_timeout_rolls_back_and_preserves_retryability(
    database: Database,
    heartbeat_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    held: str,
) -> None:
    case, _, app = heartbeat_api
    call = await call_for(app, case)
    before = await state(database)
    ledger = ConnectionLedger(database)

    async with database.transaction() as writer:
        if held == "run":
            await TransitionLocks(writer).acquire_run(case.binding.run.run_id)
        else:
            await writer.execute(select(worker.c.id).with_for_update())
        await asyncio.wait_for(call.run(app), BOUND)
    after = await state(database)

    assert call.status == 409
    assert b'"reason":"lock_timeout"' in call.body
    assert b'"retryable":true' in call.body
    assert before == after
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "shutdown"])
async def test_heartbeat_admitted_writer_settles_before_cancellation_or_shutdown(
    database: Database,
    heartbeat_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    ending: str,
) -> None:
    case, _, app = heartbeat_api
    call = await call_for(app, case)
    ledger = ConnectionLedger(database)
    closing: asyncio.Task[None] | None = None
    reached, release = asyncio.Event(), asyncio.Event()

    def before_statement(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if statement.startswith("UPDATE attempt SET") and not reached.is_set():
            reached.set()
            await_(asyncio.wait_for(release.wait(), BOUND))

    engine = database.writer_engine.sync_engine
    event.listen(engine, "before_cursor_execute", before_statement)
    pending = asyncio.create_task(call.run(app))
    try:
        await asyncio.wait_for(reached.wait(), BOUND)
        if ending == "cancel":
            pending.cancel()
        else:
            closing = asyncio.create_task(database.close(deadline_seconds=BOUND))
            await until(lambda: database.lifecycle is Lifecycle.CLOSING)
        release.set()
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, BOUND)
        else:
            await asyncio.wait_for(pending, BOUND)
            assert closing is not None
            await asyncio.wait_for(closing, BOUND)
            assert call.status == 200
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        if closing is not None:
            await closing
        event.remove(engine, "before_cursor_execute", before_statement)
    reopened = Database(database.settings)
    try:
        saved = await state(reopened)
    finally:
        await reopened.close()

    assert saved["attempt"][0]["current_dispatcher_open"] is False
    assert ledger.outstanding == database.units_in_flight() == 0
    assert ledger.closed_while_checked_out == 0
