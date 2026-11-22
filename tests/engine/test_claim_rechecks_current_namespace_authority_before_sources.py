from dataclasses import replace

import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.attempt_claims import ClaimOutcome
from kora.engine.claim_context import ClaimAccessRejected
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import (
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.asyncio
async def test_claim_rechecks_current_namespace_authority_before_sources(
    database: Database, evaluator: Evaluator, replay: bool
) -> None:
    case = await claim_case(database, evaluator)
    if replay:
        await case.claim(database)
    denied = replace(
        case, principal=case.principal.model_copy(update={"namespaces": ()})
    )
    before = await state(database)

    async def invoke() -> ClaimOutcome:
        if replay:
            return (await denied.replay(database)).outcome
        async with database.transaction() as writer:
            return (await denied.apply(writer, None)).outcome

    if replay:
        with pytest.raises(ClaimAccessRejected) as caught:
            await invoke()
        assert caught.value.reason == "forbidden"
    else:
        assert await invoke() is ClaimOutcome.INELIGIBLE
    after = await state(database)

    assert after == before
