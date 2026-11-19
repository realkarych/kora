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
from tests.engine.claim_support import (
    another_run,
    claim_case,
    state,
)

__all__ = ["evaluator"]


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
