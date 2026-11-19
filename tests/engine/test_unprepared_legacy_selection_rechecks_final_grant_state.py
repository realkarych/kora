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
    claim_case,
    state,
)

__all__ = ["evaluator"]


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
