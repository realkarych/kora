import asyncio
from dataclasses import replace

import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database, Lifecycle
from kora.db.grant_history import GrantHistoryRejected
from kora.engine.attempt_heartbeats import heartbeat_attempt
from kora.engine.locks import TransitionLocks
from kora.protocols.evidence import ClosureEvidence
from kora.protocols.worker_heartbeats import HeartbeatResponse
from tests.api.deployment_cases import evaluator
from tests.db.closing import ConnectionLedger, until
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.claim_support import WORKER_BYTES, claim_case, state
from tests.engine.heartbeat_support import (
    HEARTBEAT_LIMITS,
    heartbeat,
    heartbeat_authority,
    heartbeat_body,
    self_closure,
)
from tests.engine.test_heartbeat_observations import counters
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_heartbeat_cancellation_before_commit_rolls_back_every_owner(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    claimed = await case.claim(database)
    assert claimed.delivery is not None
    grant = claimed.delivery.system
    body = heartbeat_body(grant).model_copy(
        update={
            "counters": counters(),
            "grant_history_complete": grant.grant_history,
            "evidence": ClosureEvidence(
                runtime=None, dispatchers=(self_closure(grant),), publication=None
            ),
        }
    )
    clock = AuthorityClock()
    before = await state(database)
    ready, never_released = asyncio.Event(), asyncio.Event()
    ledger = ConnectionLedger(database)

    async def tentative() -> None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(case.binding.run.run_id)
            await heartbeat_attempt(
                heartbeat_authority(case, writer, locks, clock),
                grant.attempt_id,
                body,
                case.principal,
                HEARTBEAT_LIMITS,
                WORKER_BYTES,
            )
            ready.set()
            await never_released.wait()

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        task = tasks.create_task(tentative())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    after = await state(database)
    accepted = await heartbeat(database, case, clock, body)

    assert before == after
    assert accepted.grant_history_verified == grant.grant_history
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["native_bytes", "raw_bytes"])
async def test_heartbeat_native_budget_refusal_precedes_authority_reads(
    database: Database, evaluator: Evaluator, budget: str
) -> None:
    case = await claim_case(database, evaluator)
    claimed = await case.claim(database)
    assert claimed.delivery is not None
    grant = claimed.delivery.system
    before = await state(database)
    limits = replace(HEARTBEAT_LIMITS, **{budget: 1})

    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(case.binding.run.run_id)
        with executed_queries(writer) as statements:
            with pytest.raises(GrantHistoryRejected):
                await heartbeat_attempt(
                    heartbeat_authority(case, writer, locks, AuthorityClock()),
                    grant.attempt_id,
                    heartbeat_body(grant),
                    case.principal,
                    limits,
                    WORKER_BYTES,
                )
    after = await state(database)

    assert statements == []
    assert after == before
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_heartbeat_shutdown_waits_for_admitted_writer_and_closes_resources(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    claim = await case.claim(database)
    assert claim.delivery is not None
    grant = claim.delivery.system
    body = heartbeat_body(grant).model_copy(
        update={
            "grant_history_complete": grant.grant_history,
            "evidence": ClosureEvidence(
                runtime=None, dispatchers=(self_closure(grant),), publication=None
            ),
        }
    )
    ready, release = asyncio.Event(), asyncio.Event()
    ledger = ConnectionLedger(database)

    async def pending() -> HeartbeatResponse:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(case.binding.run.run_id)
            result = await heartbeat_attempt(
                heartbeat_authority(case, writer, locks, AuthorityClock()),
                grant.attempt_id,
                body,
                case.principal,
                HEARTBEAT_LIMITS,
                WORKER_BYTES,
            )
            ready.set()
            await release.wait()
        return result

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        writer = tasks.create_task(pending())
        await ready.wait()
        closing = tasks.create_task(database.close(deadline_seconds=10))
        try:
            await until(lambda: database.lifecycle is Lifecycle.CLOSING)
            assert not closing.done()
        finally:
            release.set()
        accepted = await writer
        await closing
    reopened = Database(database.settings)
    try:
        saved = await state(reopened)
    finally:
        await reopened.close()

    assert accepted.grant_history_verified == grant.grant_history
    assert saved["attempt"][0]["current_dispatcher_open"] is False
    assert saved["attempt_dispatcher"][0]["closed_at"] is not None
    assert database.lifecycle is Lifecycle.CLOSED
    assert ledger.outstanding == database.units_in_flight() == 0
    assert ledger.closed_while_checked_out == 0
