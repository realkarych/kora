import asyncio

import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.attempt_claims import ClaimOutcome
from tests.api.deployment_cases import evaluator
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import (
    another_run,
    claim_case,
    state,
    wait_for_claim_waiter,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_two_replicas_share_worker_capacity_across_runs(
    database: Database, evaluator: Evaluator
) -> None:
    first = await claim_case(database, evaluator)
    second = await another_run(database, first)
    prepared_first, prepared_second = (
        await first.prepare(database),
        await second.prepare(database),
    )
    decision_ready, release, contender_started = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    ledger = ConnectionLedger(database)

    async def holder() -> ClaimOutcome:
        async with database.transaction() as writer:
            result = await first.apply(writer, prepared_first)
            decision_ready.set()
            await release.wait()
        return result.outcome

    async def contender() -> ClaimOutcome:
        contender_started.set()
        async with database.transaction() as writer:
            return (await second.apply(writer, prepared_second)).outcome

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        holding = tasks.create_task(holder())
        await decision_ready.wait()
        competing = tasks.create_task(contender())
        await contender_started.wait()
        await wait_for_claim_waiter(database)
        release.set()
        results = await asyncio.gather(holding, competing)
    saved = await state(database)

    assert results == [ClaimOutcome.GRANTED, ClaimOutcome.INELIGIBLE]
    assert len(saved["attempt_dispatcher"]) == 1
    assert sum(row["current_dispatcher_open"] is True for row in saved["attempt"]) == 1
    assert database.units_in_flight() == ledger.outstanding == 0
