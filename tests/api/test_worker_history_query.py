from urllib.parse import urlencode

import pytest

from kora.api.exchange import ApiExchange
from kora.client import KoraClient
from kora.db.connection import Database
from tests.api.claim_support import BEARER, request
from tests.api.deployment_cases import evaluator
from tests.api.history_support import history_api, history_query, raw_history
from tests.engine.claim_support import ClaimCase
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator", "history_api"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
@pytest.mark.parametrize(
    "suffix",
    [
        b"&lease_epoch=1",
        b"&unknown=1",
        b"&unknown=1" * 1000,
        b"&after_epoch=%22" * 1000,
        b"&after_epoch=9223372036854775808",
        b"&after_epoch=%ZZ",
        b"&after_epoch=",
        b"&",
        b"&after_epoch=1&received_count=1",
    ],
    ids=[
        "duplicate",
        "unknown",
        "many-unknown",
        "many-invalid",
        "overflow",
        "escape",
        "empty",
        "separator",
        "duplicate-position",
    ],
)
async def test_history_raw_query_refuses_unknown_duplicate_and_invalid_scalars(
    database: Database,
    history_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    suffix: bytes,
    transport: str,
) -> None:
    case, _, app = history_api
    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        granted = (await client.claim_worker(case.query.worker_id, request(case))).claim
    assert granted is not None
    query = (
        urlencode(history_query(granted).model_dump(exclude_none=True)).encode()
        + suffix
    )
    status, body = await raw_history(
        app,
        path=f"/api/v1/attempts/{case.binding.attempt_id}/grants",
        query=query,
        headers=[(b"authorization", ("Bearer " + BEARER).encode())],
        transport=transport,
    )

    assert status == 422
    assert b"API003" in body
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
async def test_history_query_preserves_long_leading_zero_counts_without_large_integers(
    database: Database,
    history_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    transport: str,
) -> None:
    case, _, app = history_api
    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        granted = (await client.claim_worker(case.query.worker_id, request(case))).claim
    assert granted is not None
    query = urlencode(history_query(granted).model_dump(exclude_none=True)).encode()
    query = query.replace(b"lease_epoch=1", b"lease_epoch=" + b"0" * 10_000 + b"1")
    status, _body = await raw_history(
        app,
        path=f"/api/v1/attempts/{case.binding.attempt_id}/grants",
        query=query,
        headers=[(b"authorization", ("Bearer " + BEARER).encode())],
        transport=transport,
    )

    assert status == 200
    assert database.units_in_flight() == 0
