from datetime import timedelta

import pytest
from sqlalchemy import update

from kora.api.exchange import ApiExchange
from kora.client import KoraClient
from kora.client.errors import ApiError
from kora.db.connection import Database
from kora.db.tables import api_token, attempt, worker, worker_incarnation
from kora.db.tokens import REVOKED
from tests.api.claim_support import BEARER, request
from tests.api.deployment_cases import evaluator
from tests.api.history_support import history_api, history_query
from tests.client.support import running_app
from tests.db.seed import Seeder
from tests.engine.claim_support import ClaimCase, state
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator", "history_api"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("revoked", "API009"),
        ("personal", "API005"),
        ("namespace", "API001"),
        ("missing", "API001"),
        ("worker", "API005"),
        ("epoch", "API008"),
        ("expired", "API008"),
        ("incarnation", "API008"),
        ("prefix", "API002"),
        ("dispatcher", "API008"),
        ("runtime", "API005"),
        ("token", "API005"),
    ],
)
async def test_history_replay_reauthorizes_before_comparing_prefix(
    database: Database,
    history_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    change: str,
    code: str,
    transport: str,
) -> None:
    case, clock, app = history_api
    async with (
        running_app(app) as origin,
        KoraClient(
            "http://127.0.0.1" if transport == "asgi" else origin,
            BEARER,
            asgi_app=app if transport == "asgi" else None,
        ) as client,
    ):
        granted = (await client.claim_worker(case.query.worker_id, request(case))).claim
        assert granted is not None
        query = history_query(granted)
        await client.grant_history(case.binding.attempt_id, query)
        query = query.model_copy(update={"snapshot_digest": "f" * 64})
        target = case.binding.attempt_id
        async with database.transaction() as writer:
            token = update(api_token).where(api_token.c.id == case.principal.token_id)
            if change == "revoked":
                await writer.execute(token.values(expires_at=REVOKED))
            elif change == "personal":
                await writer.execute(token.values(kind="user", runtime=None))
            elif change == "namespace":
                await writer.execute(token.values(namespaces=[]))
            elif change == "worker":
                await writer.execute(token.values(owner="other-worker"))
            elif change == "runtime":
                await writer.execute(token.values(runtime="other-runtime"))
            elif change == "token":
                other = await Seeder(writer).token("a" * 64)
                await writer.execute(update(worker).values(token_id=other))
            elif change == "dispatcher":
                query = query.model_copy(update={"dispatcher_id": "z" * 26})
            elif change == "epoch":
                await writer.execute(update(attempt).values(lease_epoch=2))
            elif change == "incarnation":
                await writer.execute(
                    update(worker_incarnation).values(
                        execution_domain_id="other-domain"
                    )
                )
            elif change == "expired":
                clock.moment += timedelta(seconds=120)
            elif change == "missing":
                target = "z" * 26
        before = await state(database)

        with pytest.raises(ApiError) as caught:
            await client.grant_history(target, query)
        after = await state(database)

    assert caught.value.code == code
    assert before == after
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
