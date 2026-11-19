import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.tables import (
    attempt,
    attempt_dispatcher,
)
from kora.engine.claim_context import ClaimAccessRejected
from tests.api.deployment_cases import evaluator
from tests.db.seed import START
from tests.engine.claim_support import (
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.parametrize("damage", ["digest", "epoch", "lease"])
@pytest.mark.asyncio
async def test_receipt_conflict_and_stale_replay_do_not_mutate(
    database: Database, evaluator: Evaluator, damage: str
) -> None:
    case = await claim_case(database, evaluator)
    await case.claim(database)
    async with database.transaction() as writer:
        if damage == "digest":
            await writer.execute(
                update(attempt_dispatcher).values(claim_digest="f" * 64)
            )
        else:
            changes = (
                {"lease_epoch": 2} if damage == "epoch" else {"lease_until": START}
            )
            await writer.execute(update(attempt).values(**changes))
    before = await state(database)

    with pytest.raises(ClaimAccessRejected) as caught:
        await case.claim(database)
    after = await state(database)

    assert caught.value.reason == ("conflict" if damage == "digest" else "stale")
    assert before == after
