import asyncio
from dataclasses import dataclass, field

import pytest
from anyio import CancelScope
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kora.api.exchange import ApiExchange
from kora.client import KoraClient
from kora.db.connection import Database
from tests.api.claim_support import BEARER, request
from tests.api.deployment_cases import evaluator
from tests.api.history_support import history_api, history_query
from tests.client.support import BOUND, running_app
from tests.db.closing import ConnectionLedger, until
from tests.engine.claim_support import ClaimCase
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator", "history_api"]


@dataclass
class HeldHistory:
    app: ASGIApp
    hold_cleanup: bool = False
    held: bool = False
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    settle: asyncio.Event = field(default_factory=asyncio.Event)
    paths: list[str] = field(default_factory=list[str])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope["path"])
        self.paths.append(path)
        selected = path.endswith("/grants") and not self.held
        if selected:
            self.held = True

        async def held_send(message: Message) -> None:
            if selected and message["type"] == "http.response.start":
                self.reached.set()
                try:
                    await asyncio.wait_for(self.release.wait(), BOUND)
                finally:
                    self.closing.set()
                    if self.hold_cleanup:
                        with CancelScope(shield=True):
                            await asyncio.wait_for(self.settle.wait(), BOUND)
            await send(message)

        await self.app(scope, receive, held_send)


class HistoryClient(KoraClient):
    def admission_state(self) -> tuple[int, int]:
        return (
            len(self._history_admission.histories),
            self._history_admission.ordinary,
        )

    def request_count(self) -> int:
        return len(self._active)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
@pytest.mark.parametrize("capacity", [1, 2])
async def test_slow_history_releases_database_and_allows_same_client_claim(
    database: Database,
    history_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    transport: str,
    capacity: int,
) -> None:
    case, _, app = history_api
    held = HeldHistory(app)
    ledger = ConnectionLedger(database)

    async with running_app(held) as url:
        async with HistoryClient(
            "http://127.0.0.1" if transport == "asgi" else url,
            BEARER,
            max_connections=capacity,
            asgi_app=held if transport == "asgi" else None,
        ) as client:
            first = await client.claim_worker(case.query.worker_id, request(case))
            assert first.claim is not None
            query = history_query(first.claim)
            reading = asyncio.create_task(
                client.grant_history(case.binding.attempt_id, query)
            )
            try:
                await asyncio.wait_for(held.reached.wait(), BOUND)
                assert ledger.outstanding == database.units_in_flight() == 0
                replay = await asyncio.wait_for(
                    client.claim_worker(case.query.worker_id, request(case)), BOUND
                )
                if capacity == 1:
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(reading, BOUND)
                else:
                    assert not reading.done()
                    held.release.set()
                    page = await asyncio.wait_for(reading, BOUND)
                    assert page.complete
            finally:
                held.release.set()
                if not reading.done():
                    reading.cancel()
                await asyncio.gather(reading, return_exceptions=True)
            resumed = await client.grant_history(case.binding.attempt_id, query)

            assert first == replay
            assert resumed.complete
            assert client.admission_state() == (0, 0)
            assert client.request_count() == 0
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "close"])
async def test_cancelled_ordinary_waiter_releases_history_admission(
    database: Database,
    history_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    ending: str,
) -> None:
    case, _, app = history_api
    held = HeldHistory(app, hold_cleanup=True)
    ledger = ConnectionLedger(database)
    client = HistoryClient("http://127.0.0.1", BEARER, max_connections=1, asgi_app=held)
    first = await client.claim_worker(case.query.worker_id, request(case))
    assert first.claim is not None
    query = history_query(first.claim)
    reading = asyncio.create_task(client.grant_history(case.binding.attempt_id, query))
    await asyncio.wait_for(held.reached.wait(), BOUND)
    ordinary = asyncio.create_task(
        client.claim_worker(case.query.worker_id, request(case))
    )
    closing: asyncio.Task[None] | None = None

    try:
        await asyncio.wait_for(held.closing.wait(), BOUND)
        assert client.admission_state() == (1, 1)
        queued = asyncio.create_task(
            client.grant_history(case.binding.attempt_id, query)
        )
        await until(lambda: client.request_count() == 3)
        if ending == "cancel":
            ordinary.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(ordinary, BOUND)
            assert client.admission_state() == (1, 0)
        else:
            closing = asyncio.create_task(client.aclose())
        held.settle.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(reading, BOUND)
        if closing is None:
            page = await asyncio.wait_for(queued, BOUND)
            replay = await client.claim_worker(case.query.worker_id, request(case))
            assert page.complete
            assert replay == first
        else:
            await asyncio.wait_for(closing, BOUND)
            with pytest.raises(asyncio.CancelledError):
                await ordinary
            with pytest.raises(asyncio.CancelledError):
                await queued
    finally:
        held.release.set()
        held.settle.set()
        await client.aclose()
        await asyncio.gather(reading, ordinary, return_exceptions=True)

    assert client.admission_state() == (0, 0)
    assert client.request_count() == 0
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_queued_ordinary_claims_precede_resumed_history(
    database: Database,
    history_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
) -> None:
    case, _, app = history_api
    held = HeldHistory(app, hold_cleanup=True)
    ledger = ConnectionLedger(database)
    async with HistoryClient(
        "http://127.0.0.1", BEARER, max_connections=1, asgi_app=held
    ) as client:
        first = await client.claim_worker(case.query.worker_id, request(case))
        assert first.claim is not None
        query = history_query(first.claim)
        reading = asyncio.create_task(
            client.grant_history(case.binding.attempt_id, query)
        )
        await asyncio.wait_for(held.reached.wait(), BOUND)
        first_ordinary = asyncio.create_task(
            client.claim_worker(case.query.worker_id, request(case))
        )
        await asyncio.wait_for(held.closing.wait(), BOUND)
        queued_history = asyncio.create_task(
            client.grant_history(case.binding.attempt_id, query)
        )
        second_ordinary = asyncio.create_task(
            client.claim_worker(case.query.worker_id, request(case))
        )
        await until(
            lambda: client.admission_state() == (1, 2) and client.request_count() == 4
        )

        try:
            held.settle.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(reading, BOUND)
            responses = await asyncio.wait_for(
                asyncio.gather(first_ordinary, second_ordinary), BOUND
            )
            resumed = await asyncio.wait_for(queued_history, BOUND)
        finally:
            held.release.set()
            held.settle.set()
            await client.aclose()
            await asyncio.gather(
                reading,
                first_ordinary,
                second_ordinary,
                queued_history,
                return_exceptions=True,
            )

    assert responses == [first, first]
    assert resumed.complete
    assert [path.endswith("/claims") for path in held.paths[-3:]] == [True, True, False]
    assert client.admission_state() == (0, 0)
    assert client.request_count() == 0
    assert ledger.outstanding == database.units_in_flight() == 0
