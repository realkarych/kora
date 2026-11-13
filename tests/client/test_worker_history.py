from collections.abc import AsyncIterator

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from kora.client import KoraClient
from kora.client.errors import ClientError, Failure
from kora.models.worker_history import WorkerHistoryQuery
from kora.protocols.history import MAX_ENVELOPE_BYTES, GrantHistoryPage
from tests.client.support import (
    ProbeClient,
    Reader,
    Writer,
    peer,
    read_request,
    reply,
    running_app,
)
from tests.protocols import runtime_documents

PAGE = GrantHistoryPage.model_validate(runtime_documents.grant_page([], 0, 0))
QUERY = WorkerHistoryQuery(
    lease_epoch=1,
    dispatcher_id='dispatcher/with%?"',
    snapshot_version=1,
    snapshot_count=0,
    snapshot_digest=PAGE.snapshot.digest,
    received_count=0,
    prefix_digest=PAGE.start.prefix_digest,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
@pytest.mark.parametrize("excess", [0, 1])
async def test_history_client_bounds_complete_chunked_response_and_forwards_query(
    transport: str, excess: int
) -> None:
    body = PAGE.model_dump_json().encode()
    padded = body + b" " * (MAX_ENVELOPE_BYTES + excess - len(body))
    calls = 0

    async def chunks() -> AsyncIterator[bytes]:
        for position in range(0, len(padded), 65536):
            yield padded[position : position + 65536]

    async def endpoint(request: Request) -> Response:
        nonlocal calls
        calls += 1
        assert request.headers["authorization"] == "Bearer service"
        assert dict(request.query_params) == {
            key: str(value)
            for key, value in QUERY.model_dump(exclude_none=True).items()
        }
        return (
            StreamingResponse(chunks(), media_type="application/json")
            if calls == 1
            else Response(body, media_type="application/json")
        )

    app = Starlette(routes=[Route("/api/v1/attempts/attempt/grants", endpoint)])

    async with running_app(app) as origin:
        async with ProbeClient(
            "http://127.0.0.1" if transport == "asgi" else origin,
            "service",
            max_connections=1,
            asgi_app=app if transport == "asgi" else None,
        ) as client:
            if excess:
                with pytest.raises(ClientError) as caught:
                    await client.grant_history("attempt", QUERY)
                assert caught.value.reason is Failure.RESPONSE_LIMIT
            else:
                assert await client.grant_history("attempt", QUERY) == PAGE
            resumed = await client.grant_history("attempt", QUERY)

    assert len(padded) == MAX_ENVELOPE_BYTES + excess
    assert resumed == PAGE
    assert client.request_owners() == ()
    assert calls == 2


@pytest.mark.asyncio
async def test_history_http_truncation_releases_the_only_connection() -> None:
    body = PAGE.model_dump_json().encode()
    calls = 0

    async def serve(reader: Reader, writer: Writer) -> None:
        nonlocal calls
        calls += 1
        await read_request(reader)
        if calls == 1:
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + b"Content-Length: "
                + str(len(body) + 1).encode()
                + b"\r\n\r\n"
                + body
            )
        else:
            reply(writer, body)
        await writer.drain()

    async with peer(serve) as server:
        async with ProbeClient(server.url, "service", max_connections=1) as client:
            with pytest.raises(ClientError) as caught:
                await client.grant_history("attempt", QUERY)
            resumed = await client.grant_history("attempt", QUERY)

    assert caught.value.reason is Failure.STREAM
    assert resumed == PAGE
    assert client.request_owners() == ()
    assert calls == 2


@pytest.mark.asyncio
async def test_history_asgi_incomplete_response_releases_the_only_slot() -> None:
    calls = 0

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal calls
        calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": PAGE.model_dump_json().encode(),
                "more_body": calls == 1,
            }
        )

    async with KoraClient(
        "http://127.0.0.1", "service", asgi_app=app, max_connections=1
    ) as client:
        with pytest.raises(ClientError) as caught:
            await client.grant_history("attempt", QUERY)
        resumed = await client.grant_history("attempt", QUERY)

    assert caught.value.reason is Failure.STREAM
    assert resumed == PAGE
    assert calls == 2
