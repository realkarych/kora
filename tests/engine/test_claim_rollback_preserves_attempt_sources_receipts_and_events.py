import pytest
from sqlalchemy import select

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.tables import attempt_first_claim_basis
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
            assert tentative.delivery is not None
            basis = (
                (await writer.execute(select(attempt_first_claim_basis)))
                .mappings()
                .one()
            )
            assert basis["dispatcher_id"] == tentative.delivery.system.dispatcher_id
            assert basis["source_revision"] == 2
            raise DeliberateRollback()

    with pytest.raises(DeliberateRollback):
        await abort()
    after = await state(database)
    retried = await case.claim(database)
    committed = await state(database)

    assert before == after
    assert retried.outcome is ClaimOutcome.GRANTED
    assert retried.delivery is not None
    assert retried.delivery.system.lease_epoch == 1
    [basis] = committed["attempt_first_claim_basis"]
    assert basis["dispatcher_id"] == retried.delivery.system.dispatcher_id
    assert basis["source_revision"] == 2
