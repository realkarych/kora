import asyncio
from types import CoroutineType
from urllib.parse import urlencode

import pytest

from kora.api.exchange import ApiExchange
from kora.api.history_query import parse_history_query
from kora.client import KoraClient
from kora.db.connection import Database
from tests.api.claim_support import BEARER, request
from tests.api.deployment_cases import evaluator
from tests.api.history_support import history_api, history_query, raw_history
from tests.db.closing import ConnectionLedger, until
from tests.engine.claim_support import ClaimCase
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator", "history_api"]


def parsing(task: asyncio.Task[tuple[int, bytes]]) -> bool:
    coroutine: object = task.get_coro()
    while isinstance(coroutine, CoroutineType):
        if coroutine.cr_code is parse_history_query.__code__:
            return True
        coroutine = coroutine.cr_await
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
async def test_history_oversized_percent_encoded_scalar_is_refused_before_framework(
    database: Database,
    history_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    transport: str,
) -> None:
    case, _, app = history_api
    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        granted = (await client.claim_worker(case.query.worker_id, request(case))).claim
    assert granted is not None
    values = history_query(granted).model_dump(exclude_none=True)
    del values["dispatcher_id"]
    query = urlencode(values).encode() + b"&dispatcher_id=" + b"%22" * 1000
    ledger = ConnectionLedger(database)

    status, body = await raw_history(
        app,
        f"/api/v1/attempts/{case.binding.attempt_id}/grants",
        query,
        [(b"authorization", ("Bearer " + BEARER).encode())],
        transport,
    )

    assert status == 422
    assert b"API003" in body
    assert b"%22" not in body
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("history_api", [0.000000001], indirect=True)
async def test_history_query_deadline_refuses_before_history_acquisition(
    database: Database, history_api: tuple[ClaimCase, AuthorityClock, ApiExchange]
) -> None:
    case, _, app = history_api
    ledger = ConnectionLedger(database)

    status, body = await raw_history(
        app,
        f"/api/v1/attempts/{case.binding.attempt_id}/grants",
        b"lease_epoch=" + b"0" * 100_000,
        [(b"authorization", ("Bearer " + BEARER).encode())],
        "asgi",
    )

    assert status == 500
    assert b"API010" in body
    assert ledger.outstanding == database.units_in_flight() == 0
    assert ledger.checkouts == 1


@pytest.mark.asyncio
async def test_history_long_scalar_cancellation_releases_request_and_allows_replay(
    database: Database, history_api: tuple[ClaimCase, AuthorityClock, ApiExchange]
) -> None:
    case, _, app = history_api
    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        granted = (await client.claim_worker(case.query.worker_id, request(case))).claim
        assert granted is not None
        query = history_query(granted)
        ledger = ConnectionLedger(database)
        pending = asyncio.create_task(
            raw_history(
                app,
                f"/api/v1/attempts/{case.binding.attempt_id}/grants",
                b"lease_epoch=" + b"0" * 2_000_000,
                [(b"authorization", ("Bearer " + BEARER).encode())],
                "asgi",
            )
        )

        try:
            await until(lambda: parsing(pending))
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 10)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        resumed = await client.grant_history(case.binding.attempt_id, query)

    assert resumed.complete
    assert ledger.outstanding == database.units_in_flight() == 0
