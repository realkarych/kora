import asyncio

import pytest

from kora.client import ClientError, KoraClient
from kora.client.errors import Failure
from kora.ir.json import JsonObject
from tests.client.support import (
    BOUND,
    CREDENTIAL,
    ProbeClient,
    Reader,
    Writer,
    peer,
    read_request,
    reply,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["deadline", "caller"])
async def test_client_releases_requests(stop: str) -> None:
    started = asyncio.Event()
    disconnected = asyncio.Event()
    requests = 0

    async def handle(reader: Reader, writer: Writer) -> None:
        nonlocal requests
        await read_request(reader)
        requests += 1
        if requests == 1:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{")
            await writer.drain()
            started.set()
            assert await asyncio.wait_for(reader.read(), BOUND) == b""
            disconnected.set()
        else:
            reply(writer)

    async with (
        peer(handle) as origin,
        KoraClient(
            origin.url, CREDENTIAL, request_timeout=1.0, max_connections=1
        ) as client,
    ):
        request = asyncio.create_task(client.openapi())
        await asyncio.wait_for(started.wait(), BOUND)
        if stop == "deadline":
            with pytest.raises(ClientError) as timeout:
                await request
            assert timeout.value.reason is Failure.DEADLINE
        else:
            request.cancel()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        await asyncio.wait_for(disconnected.wait(), BOUND)
        schema = await client.openapi()

    assert requests == 2
    assert request.done()
    assert schema["openapi"] == "3.1.0"


@pytest.mark.asyncio
async def test_client_closes_pool_with_active_and_queued_requests() -> None:
    started = asyncio.Event()
    disconnected = asyncio.Event()
    requests = 0

    async def handle(reader: Reader, writer: Writer) -> None:
        nonlocal requests
        await read_request(reader)
        requests += 1
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{")
        await writer.drain()
        started.set()
        assert await asyncio.wait_for(reader.read(), BOUND) == b""
        disconnected.set()

    async with (
        peer(handle) as origin,
        ProbeClient(origin.url, CREDENTIAL, max_connections=1) as client,
    ):
        active = asyncio.create_task(client.openapi())
        await asyncio.wait_for(started.wait(), BOUND)

        admitted = asyncio.Event()

        async def queued() -> JsonObject:
            asyncio.get_running_loop().call_later(0, admitted.set)
            return await client.openapi()

        waiting = asyncio.create_task(queued())
        await asyncio.wait_for(admitted.wait(), BOUND)
        owners = client.request_owners()
        closer = asyncio.create_task(client.aclose())
        other_closer = asyncio.create_task(client.aclose())
        await asyncio.wait_for(other_closer, BOUND)
        await asyncio.wait_for(closer, BOUND)
        outcomes = await asyncio.wait_for(
            asyncio.gather(active, waiting, return_exceptions=True), BOUND
        )
        await asyncio.wait_for(disconnected.wait(), BOUND)
        with pytest.raises(RuntimeError, match="closed"):
            await client.openapi()

    assert len(owners) == 2
    assert all(owner.done() for owner in owners)
    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
    assert requests == 1


@pytest.mark.asyncio
async def test_client_closes_pool_and_does_not_store_cookies() -> None:
    disconnected = asyncio.Event()
    seen: list[bytes] = []

    async def handle(reader: Reader, writer: Writer) -> None:
        seen.append(await read_request(reader))
        reply(writer, headers=(b"Set-Cookie: peer=private-cookie; Path=/",))
        await writer.drain()
        seen.append(await read_request(reader))
        reply(writer)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), BOUND) == b""
        disconnected.set()

    async with peer(handle) as origin:
        async with KoraClient(origin.url, CREDENTIAL, max_connections=1) as client:
            first = await client.openapi()
            second = await client.openapi()
            assert not disconnected.is_set()
        await asyncio.wait_for(disconnected.wait(), BOUND)

    assert first == second
    assert len(seen) == 2
    assert all(b"cookie:" not in request.lower() for request in seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["deadline", "caller", "close"])
async def test_client_releases_socket_during_tls_handshake(stop: str) -> None:
    started = asyncio.Event()
    disconnected = asyncio.Event()
    handshake: list[bytes] = []

    async def stall_handshake(reader: Reader, _writer: Writer) -> None:
        handshake.append(await reader.read(4096))
        started.set()
        handshake.append(await asyncio.wait_for(reader.read(), BOUND))
        disconnected.set()

    async with (
        peer(stall_handshake) as origin,
        KoraClient(
            origin.url.replace("http:", "https:"), CREDENTIAL, request_timeout=1.0
        ) as client,
    ):
        request = asyncio.create_task(client.openapi())
        await asyncio.wait_for(started.wait(), BOUND)
        if stop == "deadline":
            with pytest.raises(ClientError) as timeout:
                await request
            assert timeout.value.reason is Failure.DEADLINE
            assert timeout.value.outcome == "not_sent"
        else:
            if stop == "caller":
                request.cancel()
            else:
                await client.aclose()
            with pytest.raises(asyncio.CancelledError):
                await request
        await asyncio.wait_for(disconnected.wait(), BOUND)

    assert handshake[0].startswith(b"\x16\x03")
    assert handshake[1] == b""
    assert request.done()
