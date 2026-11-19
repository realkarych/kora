import asyncio
from typing import cast

import pytest
from sqlalchemy import select

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.tables import (
    worker,
)
from kora.engine.attempt_claims import ClaimOutcome
from kora.ir.json import JsonObject
from tests.api.deployment_cases import evaluator, skeleton_ir
from tests.engine.claim_support import (
    another_run,
    claim_case,
    state,
    wait_for_claim_waiter,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_parent_expiry_uses_time_after_worker_guard_wait(
    database: Database, evaluator: Evaluator
) -> None:
    ir = skeleton_ir()
    runtime = cast(JsonObject, ir["runtime"])
    runtime["limits"] = {"timeout": "1s"}
    case = await claim_case(database, evaluator, ir)
    other = await another_run(database, case)
    prepared = await other.prepare(database)
    before = await state(database)
    guarded, release = asyncio.Event(), asyncio.Event()

    async def holder() -> None:
        async with database.transaction() as writer:
            await case.context(writer)
            await writer.execute(
                select(worker.c.id)
                .where(worker.c.id == case.query.worker_id)
                .with_for_update()
            )
            guarded.set()
            await release.wait()

    async def contender() -> ClaimOutcome:
        async with database.transaction() as writer:
            return (await other.apply(writer, prepared)).outcome

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        held = tasks.create_task(holder())
        await guarded.wait()
        pending = tasks.create_task(contender())
        await wait_for_claim_waiter(database)
        case.clock.advance(1)
        release.set()
        await held
        outcome = await pending
    after = await state(database)

    assert outcome is ClaimOutcome.EXPIRE
    assert before == after
    assert database.units_in_flight() == 0
