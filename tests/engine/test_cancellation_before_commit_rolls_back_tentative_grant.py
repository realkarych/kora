import asyncio

import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.attempt_claims import ClaimOutcome
from tests.api.deployment_cases import evaluator
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import (
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_cancellation_before_commit_rolls_back_tentative_grant(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    prepared = await case.prepare(database)
    before = await state(database)
    tentative_ready, never_released = asyncio.Event(), asyncio.Event()
    ledger = ConnectionLedger(database)

    async def cancel_before_commit() -> None:
        async with database.transaction() as writer:
            result = await case.apply(writer, prepared)
            assert result.outcome is ClaimOutcome.GRANTED
            tentative_ready.set()
            await never_released.wait()

    async with asyncio.timeout(10):
        task = asyncio.create_task(cancel_before_commit())
        await tentative_ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    after = await state(database)
    retry = await case.claim(database)

    assert before == after
    assert retry.outcome is ClaimOutcome.GRANTED
    assert database.units_in_flight() == ledger.outstanding == 0
