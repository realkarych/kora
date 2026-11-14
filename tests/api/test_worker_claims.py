from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import delete, update

from kora.api.exchange import ApiExchange
from kora.client import KoraClient
from kora.client.errors import ApiError
from kora.db.connection import Database
from kora.db.tables import api_token, attempt_claim_read_admission
from kora.db.tokens import REVOKED
from kora.ir.json import parse_json_object
from kora.models.errors import ApiErrorCode
from tests.api.claim_support import BEARER, claim_api, request
from tests.api.deployment_cases import evaluator
from tests.api.support import client_for
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.claim_support import ClaimCase, another_run, state
from tests.plugins.authentication.support import AuthorityClock
from tests.support.parity import schema_operations, uncovered_operations

__all__ = ["claim_api", "evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
async def test_http_claim_and_replay_share_atomic_receipt_without_writes(
    database: Database,
    claim_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    transport: str,
) -> None:
    case, clock, app = claim_api
    ledger = ConnectionLedger(database)

    async with running_app(app) as url:
        async with KoraClient(
            "http://127.0.0.1" if transport == "asgi" else url,
            BEARER,
            asgi_app=app if transport == "asgi" else None,
        ) as client:
            granted = await client.claim_worker(case.query.worker_id, request(case))
            before = await state(database)
            clock.moment += timedelta(seconds=20)
            replayed = await client.claim_worker(case.query.worker_id, request(case))
    after = await state(database)

    assert granted.claim is not None
    assert replayed == granted
    assert granted.claim.attempt_id == case.binding.attempt_id
    assert granted.claim.permitted_actions == ("seal_request",)
    assert granted.claim.limits["stall_after_us"] == "17"
    assert before == after
    assert after["worker"][0]["claim_cursor"] == case.binding.attempt_id
    assert after["worker"][0]["claim_cursor_generation"] == 1
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_http_claim_full_worker_returns_null_and_expired_replay_is_stale(
    database: Database, claim_api: tuple[ClaimCase, AuthorityClock, ApiExchange]
) -> None:
    case, clock, app = claim_api

    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        first = await client.claim_worker(case.query.worker_id, request(case))
        empty = await client.claim_worker(
            case.query.worker_id, request(case, "another")
        )
        clock.moment += timedelta(seconds=121)
        before = await state(database)
        with pytest.raises(ApiError) as refused:
            await client.claim_worker(case.query.worker_id, request(case))
    after = await state(database)

    assert first.claim is not None
    assert empty.claim is None
    assert refused.value.code == ApiErrorCode.STALE_GRANT
    assert before == after


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["owner", "runtime", "incarnation", "domain", "revoked", "user", "body"]
)
async def test_claim_http_authority_refusal_has_no_effect(
    database: Database,
    claim_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    change: str,
) -> None:
    case, _clock, app = claim_api
    body = request(case).model_dump(mode="json")
    path = "/api/v1/workers/claim-worker/claims"
    expected = 403
    if change == "owner":
        path = "/api/v1/workers/foreign/claims"
    elif change in ("runtime", "incarnation", "domain"):
        field = {
            "runtime": "runtime",
            "incarnation": "incarnation_id",
            "domain": "execution_domain_id",
        }[change]
        body[field] = "foreign"
    elif change == "body":
        body["max"] = 2
        expected = 422
    else:
        async with database.transaction() as writer:
            await writer.execute(
                update(api_token)
                .where(api_token.c.id == case.principal.token_id)
                .values(
                    **(
                        {"expires_at": REVOKED}
                        if change == "revoked"
                        else {"kind": "user", "runtime": None}
                    )
                )
            )
        if change == "revoked":
            expected = 401
    before = await state(database)

    async with client_for(app) as client:
        refused = await client.post(
            path, json=body, headers={"Authorization": "Bearer " + BEARER}
        )
    after = await state(database)

    assert refused.status_code == expected
    assert before == after
    assert b"foreign" not in refused.content
    assert BEARER.encode() not in refused.content


@pytest.mark.asyncio
async def test_claim_refused_source_advances_past_legacy_to_valid_tail(
    database: Database, claim_api: tuple[ClaimCase, AuthorityClock, ApiExchange]
) -> None:
    case, _clock, app = claim_api
    tail = await another_run(database, case)
    async with database.transaction() as writer:
        await writer.execute(
            delete(attempt_claim_read_admission).where(
                attempt_claim_read_admission.c.attempt_id == case.binding.attempt_id
            )
        )
    before = await state(database)

    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        with pytest.raises(ApiError) as refusal:
            await client.claim_worker(case.query.worker_id, request(case))
        intermediate = await state(database)
        granted = await client.claim_worker(
            case.query.worker_id, request(case, "valid-tail")
        )

    assert refusal.value.code == ApiErrorCode.INTERNAL
    assert intermediate["worker"][0]["claim_cursor"] == case.binding.attempt_id
    assert intermediate["attempt_dispatcher"] == before["attempt_dispatcher"]
    assert intermediate["attempt"] == before["attempt"]
    assert granted.claim is not None
    assert granted.claim.attempt_id == tail.binding.attempt_id


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace_scope", ["empty", "foreign"])
async def test_claim_http_rechecks_namespace_scope_for_replay_and_fresh_work(
    database: Database,
    claim_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    namespace_scope: str,
) -> None:
    case, _clock, app = claim_api
    tail = await another_run(database, case)
    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        first = await client.claim_worker(case.query.worker_id, request(case))
    async with database.transaction() as writer:
        foreign = await Seeder(writer).namespace("foreign-claim-scope")
        await writer.execute(
            update(api_token)
            .where(api_token.c.id == case.principal.token_id)
            .values(
                namespaces=[]
                if namespace_scope == "empty"
                else [{"namespace_id": foreign, "transcripts": False}]
            )
        )
        await writer.execute(
            delete(attempt_claim_read_admission).where(
                attempt_claim_read_admission.c.attempt_id == tail.binding.attempt_id
            )
        )
    before = await state(database)

    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        with pytest.raises(ApiError) as refusal:
            await client.claim_worker(case.query.worker_id, request(case))
        after_replay = await state(database)
        empty = await client.claim_worker(case.query.worker_id, request(case, "fresh"))
    after = await state(database)

    assert first.claim is not None
    assert refusal.value.code == ApiErrorCode.FORBIDDEN
    assert before == after_replay
    assert empty.claim is None
    assert after["attempt"] == before["attempt"]
    assert after["attempt_dispatcher"] == before["attempt_dispatcher"]
    assert after["event"] == before["event"]
    assert after["worker"][0]["claim_cursor"] == tail.binding.attempt_id


@pytest.mark.asyncio
async def test_claim_endpoint_is_explicitly_worker_only_in_parity(
    claim_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
) -> None:
    _case, _clock, app = claim_api
    assert isinstance(app.application, FastAPI)

    schema = parse_json_object(app.application.openapi())
    paths = schema["paths"]

    assert isinstance(paths, dict)
    assert ("POST", "/api/v1/workers/{}/claims") in schema_operations(paths)
    assert (
        uncovered_operations(paths, Path(__file__).resolve().parents[2] / "src") == []
    )
    assert uncovered_operations(
        {"/api/v1/workers/{id}/claims": {"get": {}}},
        Path(__file__).resolve().parents[2] / "src",
    ) == ["cli: GET /api/v1/workers/{}/claims", "mcp: GET /api/v1/workers/{}/claims"]
