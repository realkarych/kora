import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database
from kora.db.tables import (
    attempt,
)
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import (
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_unknown_segment_offset_rolls_back_first_claim(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    async with database.transaction() as writer:
        await writer.execute(update(attempt).values(acknowledged_segment_offset=None))
    before = await state(database)

    with pytest.raises(ClaimSourcesRejected):
        await case.claim(database)
    after = await state(database)

    assert before == after
