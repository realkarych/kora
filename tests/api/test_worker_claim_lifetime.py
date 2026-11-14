import asyncio
from dataclasses import dataclass, field

import pytest
from starlette.types import Message

from kora.api.exchange import ApiExchange
from kora.client import KoraClient
from kora.db.connection import Database
from tests.api.claim_support import BEARER, claim_api, request
from tests.api.deployment_cases import evaluator
from tests.api.support import HttpCall
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import ClaimCase, state
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["claim_api", "evaluator"]


@dataclass
class HeldClaimResponse(HttpCall):
    payload: bytes = b""
    lose_response: bool = False
    sending: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def receive(self) -> Message:
        self.receives += 1
        return {"type": "http.request", "body": self.payload, "more_body": False}

    async def send(self, message: Message) -> None:
        await super().send(message)
        if message["type"] == "http.response.start":
            self.sending.set()
            await self.release.wait()
            if self.lose_response:
                raise ConnectionResetError("claim response receiver disconnected")


@pytest.mark.asyncio
@pytest.mark.parametrize("receiver", ["slow", "lost", "cancelled"])
async def test_claim_settles_before_response_delivery_and_replays_after_loss(
    database: Database,
    claim_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    receiver: str,
) -> None:
    case, _clock, app = claim_api
    call = HeldClaimResponse(
        path="/api/v1/workers/claim-worker/claims",
        method="POST",
        headers=[
            (b"authorization", ("Bearer " + BEARER).encode()),
            (b"content-type", b"application/json"),
        ],
        payload=request(case).model_dump_json().encode(),
        lose_response=receiver == "lost",
    )
    ledger = ConnectionLedger(database)

    pending = asyncio.create_task(call.run(app))
    try:
        async with asyncio.timeout(10):
            await call.sending.wait()
            held_units = database.units_in_flight()
            held_connections = ledger.outstanding
            committed = await state(database)
            async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
                replayed = await client.claim_worker(
                    case.query.worker_id, request(case)
                )
            if receiver == "cancelled":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            elif receiver == "lost":
                call.release.set()
                with pytest.raises(ConnectionResetError):
                    await pending
            else:
                call.release.set()
                await pending
            async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
                after_delivery = await client.claim_worker(
                    case.query.worker_id, request(case)
                )
        after = await state(database)
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    assert call.status == 200
    assert held_units == held_connections == 0
    assert len(committed["attempt_dispatcher"]) == 1
    assert committed == after
    assert replayed.claim is not None
    assert after_delivery == replayed
    assert replayed.claim.attempt_id == case.binding.attempt_id
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
