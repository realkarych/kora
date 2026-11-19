from dataclasses import replace

import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.attempt_claims import ClaimOutcome
from kora.engine.claim_context import ClaimAccessRejected
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import (
    another_run,
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_receipt_redirect_and_stale_refusal_precede_source_preparation(
    database: Database, evaluator: Evaluator
) -> None:
    first = await claim_case(database, evaluator)
    second = await another_run(database, first)
    await first.claim(database)
    replay_from_other_run = replace(second, query=first.query)
    before = await state(database)

    async with database.transaction() as writer:
        redirect = await replay_from_other_run.apply(writer, None)
    first.clock.advance(121)

    async def stale_without_sources() -> None:
        async with database.transaction() as writer:
            await first.apply(writer, None)

    with pytest.raises(ClaimAccessRejected) as caught:
        await stale_without_sources()
    after = await state(database)

    assert redirect.outcome is ClaimOutcome.REDIRECT
    assert redirect.redirect == first.binding.attempt_id
    assert caught.value.reason == "stale"
    assert before == after
