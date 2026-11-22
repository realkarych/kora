import pytest
from sqlalchemy import update

from kora.api.exchange import ApiExchange
from kora.client import KoraClient
from kora.client.errors import ApiError
from kora.db.connection import Database
from kora.db.tables import (
    attempt,
    attempt_dispatcher,
)
from kora.engine.claim_context import ClaimAccessRejected
from kora.models.errors import ApiErrorCode
from tests.api.claim_support import BEARER, claim_api, request
from tests.api.deployment_cases import evaluator
from tests.db.seed import START
from tests.engine.claim_support import (
    ClaimCase,
    state,
)
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["claim_api", "evaluator"]


@pytest.mark.parametrize("damage", ["digest", "epoch", "lease", "digest-and-lease"])
@pytest.mark.asyncio
async def test_receipt_conflict_and_stale_replay_do_not_mutate(
    database: Database,
    claim_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    damage: str,
) -> None:
    case, _, app = claim_api
    await case.claim(database)
    conflicting_digest = damage in ("digest", "digest-and-lease")
    async with database.transaction() as writer:
        if conflicting_digest:
            await writer.execute(
                update(attempt_dispatcher).values(claim_digest="f" * 64)
            )
        if damage != "digest":
            changes = (
                {"lease_epoch": 2} if damage == "epoch" else {"lease_until": START}
            )
            await writer.execute(update(attempt).values(**changes))
    before = await state(database)

    with pytest.raises(ClaimAccessRejected) as caught:
        await case.claim(database)
    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        with pytest.raises(ApiError) as refused:
            await client.claim_worker(case.query.worker_id, request(case))
    after = await state(database)

    assert caught.value.reason == ("conflict" if conflicting_digest else "stale")
    assert refused.value.code == (
        ApiErrorCode.CONFLICT if conflicting_digest else ApiErrorCode.STALE_GRANT
    )
    assert before == after
