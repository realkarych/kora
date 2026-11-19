from dataclasses import replace

import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.grant_history_v1 import ACK_FIELDS, TIP_FIELDS
from kora.db.tables import (
    attempt,
    attempt_dispatcher,
)
from kora.engine.attempt_claims import ClaimOutcome
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import (
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_history_maintenance_commits_without_issuing_a_grant(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    await case.claim(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt_dispatcher).values(
                prefix_count=None, prefix_digest=None, claim_id=None, claim_digest=None
            )
        )
        await writer.execute(
            update(attempt).values(**dict.fromkeys((*TIP_FIELDS, *ACK_FIELDS)))
        )
    case.clock.advance(121)
    next_case = replace(
        case, query=case.query.model_copy(update={"claim_id": "history"})
    )
    before = await state(database)

    maintained = await next_case.claim(database)
    after = await state(database)
    granted = await next_case.claim(database)

    assert maintained.outcome is ClaimOutcome.MAINTAINED
    assert maintained.delivery is None
    assert after["attempt"][0]["lease_epoch"] == before["attempt"][0]["lease_epoch"]
    assert (
        after["attempt"][0]["recovery_version"]
        == before["attempt"][0]["recovery_version"]
    )
    assert after["event"] == before["event"]
    assert len(after["attempt_dispatcher"]) == 1
    assert after["attempt_dispatcher"][0]["prefix_count"] == 1
    assert after["attempt_dispatcher"][0]["claim_id"] is None
    assert granted.outcome is ClaimOutcome.GRANTED
    assert granted.delivery is not None
    assert granted.delivery.system.grant_history.count == 2
