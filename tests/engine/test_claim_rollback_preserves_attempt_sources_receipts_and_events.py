import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.attempt_claims import ClaimOutcome
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import (
    claim_case,
    state,
)
from tests.engine.lock_support import DeliberateRollback

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_claim_rollback_preserves_attempt_sources_receipts_and_events(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    prepared = await case.prepare(database)
    before = await state(database)

    async def abort() -> None:
        async with database.transaction() as writer:
            tentative = await case.apply(writer, prepared)
            assert tentative.outcome is ClaimOutcome.GRANTED
            raise DeliberateRollback()

    with pytest.raises(DeliberateRollback):
        await abort()
    after = await state(database)
    retried = await case.claim(database)

    assert before == after
    assert retried.outcome is ClaimOutcome.GRANTED
    assert retried.delivery is not None
    assert retried.delivery.system.lease_epoch == 1
