import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.attempt_heartbeats import heartbeat_attempt
from kora.engine.attempt_usage import report_usage
from kora.engine.locks import TransitionLocks
from kora.engine.usage_acceptance import UsageRefusal, UsageRejected
from kora.engine.usage_preparation import read_usage_context
from kora.protocols.worker_heartbeats import WorkerHeartbeat
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import (
    WORKER_BYTES,
    claim_case,
    state,
    wait_for_claim_waiter,
)
from tests.engine.heartbeat_support import (
    HEARTBEAT_LIMITS,
    heartbeat_authority,
    heartbeat_body,
)
from tests.engine.test_graph_execution_contract import LIMITS
from tests.engine.test_heartbeat_observations import counters
from tests.engine.test_usage_acceptance import coverage
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeat_first", [False, True])
@pytest.mark.parametrize("regression", [False, True])
async def test_usage_and_heartbeat_share_coverage_revision_under_the_run_lock(
    database: Database, evaluator: Evaluator, heartbeat_first: bool, regression: bool
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    clock = AuthorityClock()
    context = await read_usage_context(
        database,
        granted.attempt_id,
        granted.current_grant.dispatcher_id,
        granted.lease_epoch,
        case.principal,
        clock,
        LIMITS,
    )
    body = heartbeat_body(granted)
    later_sequence = 0 if regression else 2

    async def apply(
        connection: AsyncConnection,
        locks: TransitionLocks,
        is_heartbeat: bool,
        sequence: int,
    ) -> None:
        observation = coverage(sequence, observed_at=clock.now())
        if is_heartbeat:
            request = WorkerHeartbeat.model_validate(
                {
                    **body.model_dump(),
                    "counters": counters().model_copy(
                        update={"usage_coverage": observation}
                    ),
                }
            )
            await heartbeat_attempt(
                heartbeat_authority(case, connection, locks, clock),
                granted.attempt_id,
                request,
                case.principal,
                HEARTBEAT_LIMITS,
                WORKER_BYTES,
            )
        else:
            await report_usage(
                connection,
                locks,
                context,
                granted.attempt_id,
                granted.current_grant.dispatcher_id,
                granted.lease_epoch,
                case.principal,
                (),
                observation,
                clock,
                case.identifiers,
            )

    async def compete() -> UsageRefusal | None:
        try:
            async with database.transaction() as writer:
                locks = TransitionLocks(writer)
                await locks.acquire_run(case.binding.run.run_id)
                await apply(writer, locks, not heartbeat_first, later_sequence)
        except UsageRejected as error:
            return error.reason
        return None

    async with asyncio.timeout(10), asyncio.TaskGroup() as group:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(case.binding.run.run_id)
            pending = group.create_task(compete())
            await wait_for_claim_waiter(database)
            await apply(writer, locks, heartbeat_first, 1)
    saved = await state(database)

    assert pending.result() is (UsageRefusal.REGRESSION if regression else None)
    assert saved["attempt"][0]["usage_coverage"] == coverage(
        1 if regression else 2, observed_at=clock.now()
    ).model_dump(mode="json")
    if regression and not heartbeat_first:
        assert saved["attempt"][0]["counters"] == {}
        assert saved["attempt"][0]["last_heartbeat_at"] is None
    assert saved["attempt"][0]["acknowledged_segment_offset"] == 0
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
