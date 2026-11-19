from dataclasses import replace

import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.claim_context import ClaimAccessRejected
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import (
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.parametrize("change", ["incarnation", "domain", "token"])
@pytest.mark.asyncio
async def test_claim_refuses_foreign_worker_bindings_before_sources(
    database: Database, evaluator: Evaluator, change: str
) -> None:
    case = await claim_case(database, evaluator)
    denied = (
        replace(
            case,
            principal=case.principal.model_copy(update={"token_id": "foreign-token"}),
        )
        if change == "token"
        else replace(
            case,
            query=case.query.model_copy(
                update={
                    "incarnation_id"
                    if change == "incarnation"
                    else "execution_domain_id": "foreign-identity"
                }
            ),
        )
    )
    before = await state(database)

    async def invoke() -> None:
        async with database.transaction() as writer:
            await denied.apply(writer, None)

    with pytest.raises(ClaimAccessRejected) as caught:
        await invoke()
    after = await state(database)

    assert caught.value.reason == "forbidden"
    assert after == before
