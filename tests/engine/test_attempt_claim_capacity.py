import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.tables import (
    attempt,
)
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


@pytest.mark.asyncio
async def test_live_legacy_projection_reconciliation_is_one_terminal_transition(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    second = await another_run(database, case)
    await case.claim(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == case.binding.attempt_id)
            .values(current_dispatcher_open=None)
        )
    next_case = replace(
        case, query=case.query.model_copy(update={"claim_id": "reconcile"})
    )
    before = await state(database)

    blocked = await second.claim(database)
    async with database.transaction() as writer:
        reconciled = await next_case.apply(writer, None)
    after = await state(database)

    assert blocked.outcome is ClaimOutcome.INELIGIBLE
    assert reconciled.outcome is ClaimOutcome.MAINTAINED
    assert reconciled.delivery is None
    assert after["attempt_dispatcher"] == before["attempt_dispatcher"]
    assert after["event"] == before["event"]
    assert (
        after["attempt_claim_read_admission"] == before["attempt_claim_read_admission"]
    )
    changed = [row for row in after["attempt"] if row["id"] == case.binding.attempt_id]
    assert len(changed) == 1
    assert changed[0]["current_dispatcher_open"] is True
    assert changed[0]["lease_epoch"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["known", "expired", "receipt"])
async def test_unprepared_legacy_selection_rechecks_final_grant_state(
    database: Database, evaluator: Evaluator, changed: str
) -> None:
    case = await claim_case(database, evaluator)
    await case.claim(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt).values(
                current_dispatcher_open=True if changed == "known" else None
            )
        )
    if changed == "expired":
        case.clock.advance(121)
    selected = (
        case
        if changed == "receipt"
        else replace(case, query=case.query.model_copy(update={"claim_id": "legacy"}))
    )
    before = await state(database)

    async with database.transaction() as writer:
        result = await selected.apply(writer, None)
    after = await state(database)

    assert result.outcome is (
        ClaimOutcome.REDIRECT if changed == "receipt" else ClaimOutcome.INELIGIBLE
    )
    assert result.redirect == (
        case.binding.attempt_id if changed == "receipt" else None
    )
    assert result.delivery is None
    assert after == before
