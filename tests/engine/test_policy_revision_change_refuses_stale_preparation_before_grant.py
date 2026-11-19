import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database
from kora.db.tables import (
    namespace,
)
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import (
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_policy_revision_change_refuses_stale_preparation_before_grant(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    prepared = await case.prepare(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(namespace).values(policy_revision=namespace.c.policy_revision + 1)
        )
    before = await state(database)

    async def invoke() -> None:
        async with database.transaction() as writer:
            await case.apply(writer, prepared)

    with pytest.raises(ClaimSourcesRejected):
        await invoke()
    after = await state(database)

    assert before == after
