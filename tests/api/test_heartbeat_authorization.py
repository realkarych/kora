from datetime import timedelta

import pytest
from sqlalchemy import update

from kora.api.exchange import ApiExchange
from kora.client import KoraClient
from kora.client.errors import ApiError
from kora.db.connection import Database
from kora.db.tables import api_token, attempt, worker, worker_incarnation
from kora.db.tokens import REVOKED
from kora.protocols.worker_heartbeats import WorkerHeartbeat
from tests.api.claim_support import BEARER, request
from tests.api.deployment_cases import evaluator
from tests.api.heartbeat_support import heartbeat_api
from tests.api.support import HttpCall, client_for
from tests.client.support import running_app
from tests.db.seed import Seeder
from tests.engine.claim_support import ClaimCase, state
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator", "heartbeat_api"]


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
async def test_heartbeat_reauthorizes_before_accepting_history(
    database: Database,
    heartbeat_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    change: str,
    code: str,
    transport: str,
) -> None:
    case, clock, app = heartbeat_api
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
        body = WorkerHeartbeat(
            dispatcher_id=granted.current_grant.dispatcher_id,
            lease_epoch=granted.lease_epoch,
            grant_history_complete=granted.grant_history.model_copy(
                update={"digest": "f" * 64}
            ),
        )
        target = granted.attempt_id
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
                body = body.model_copy(update={"dispatcher_id": "z" * 26})
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
            await client.heartbeat_attempt(target, body)
        after = await state(database)

    assert caught.value.code == code
    assert before == after
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["missing", "revoked", "personal"])
async def test_heartbeat_authentication_precedes_body_consumption(
    database: Database,
    heartbeat_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    credential: str,
) -> None:
    case, _, app = heartbeat_api
    async with database.transaction() as writer:
        statement = update(api_token).where(api_token.c.id == case.principal.token_id)
        if credential == "revoked":
            await writer.execute(statement.values(expires_at=REVOKED))
        elif credential == "personal":
            await writer.execute(statement.values(kind="user", runtime=None))
    call = HttpCall(
        path=f"/api/v1/attempts/{case.binding.attempt_id}/heartbeat",
        method="POST",
        headers=[]
        if credential == "missing"
        else [(b"authorization", ("Bearer " + BEARER).encode())],
    )

    await call.run(app)

    assert call.receives == 0
    assert call.status == (403 if credential == "personal" else 401)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["unknown", "epoch", "bytes", "identifier"])
async def test_heartbeat_invalid_body_and_path_preserve_state(
    database: Database,
    heartbeat_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    invalid: str,
) -> None:
    case, _, app = heartbeat_api
    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        granted = (await client.claim_worker(case.query.worker_id, request(case))).claim
    assert granted is not None
    body: dict[str, object] = {
        "lease_epoch": True if invalid == "epoch" else granted.lease_epoch,
        "dispatcher_id": granted.current_grant.dispatcher_id,
    }
    if invalid == "unknown":
        body["private-payload"] = "secret"
    target = "%00" if invalid == "identifier" else granted.attempt_id
    path = f"/api/v1/attempts/{target}/heartbeat"
    before = await state(database)

    async with client_for(app) as raw:
        options = {"Authorization": "Bearer " + BEARER}
        response = (
            await raw.post(path, content=b"x" * (4 * 1024 * 1024 + 1), headers=options)
            if invalid == "bytes"
            else await raw.post(path, json=body, headers=options)
        )
    after = await state(database)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "API003"
    assert "secret" not in response.text
    assert "private-payload" not in response.text
    assert before == after
