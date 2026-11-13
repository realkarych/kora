from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import update

from kora.api.exchange import ApiExchange
from kora.client import KoraClient
from kora.db.connection import Database
from kora.db.tables import api_token
from kora.engine.worker_registration import register_worker
from tests.api.claim_support import BEARER, request
from tests.api.deployment_cases import evaluator
from tests.api.history_support import history_api, history_query, raw_history
from tests.client.support import running_app
from tests.engine.claim_support import ClaimCase
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator", "history_api"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
@pytest.mark.parametrize("credential", ["missing", "cookie", "unknown", "expired"])
async def test_history_authentication_precedes_raw_query_validation(
    database: Database,
    history_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    transport: str,
    credential: str,
) -> None:
    case, clock, app = history_api
    headers: list[tuple[bytes, bytes]] = []
    if credential == "cookie":
        headers = [(b"cookie", ("Authorization=Bearer " + BEARER).encode())]
    elif credential == "unknown":
        headers = [(b"authorization", b"Bearer unknown-private-token")]
    elif credential == "expired":
        headers = [(b"authorization", ("Bearer " + BEARER).encode())]
        async with database.transaction() as writer:
            await writer.execute(
                update(api_token)
                .where(api_token.c.id == case.principal.token_id)
                .values(expires_at=clock.now() - timedelta(seconds=1))
            )

    status, body = await raw_history(
        app,
        f"/api/v1/attempts/{case.binding.attempt_id}/grants",
        b"unknown=private-history-query",
        headers,
        transport,
    )

    assert status == 401
    assert b"API009" in body
    assert b"private-" not in body
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
@pytest.mark.parametrize("domain", ["same", "new"])
async def test_new_registration_preserves_the_current_retained_history_grant(
    database: Database,
    history_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    transport: str,
    domain: str,
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
        previous = await client.grant_history(case.binding.attempt_id, query)
        next_registration = replace(
            case.registration,
            incarnation_id="next-incarnation",
            execution_domain_id=(
                case.registration.execution_domain_id
                if domain == "same"
                else "next-domain"
            ),
            process=case.registration.process.model_copy(update={"pid": 999}),
        )
        async with database.transaction() as writer:
            await register_worker(
                writer,
                next_registration,
                case.principal,
                portable=False,
                now=clock.now(),
                event_id=case.identifiers.new(),
            )

        retained = await client.grant_history(case.binding.attempt_id, query)

    assert retained == previous
    assert retained.grants == (granted.current_grant,)
    assert database.units_in_flight() == 0
