import asyncio
import socket

import httpx
import pytest
from anyio import CancelScope
from typing_extensions import Buffer

from kora.client import ClientError, KoraClient
from kora.client.errors import Failure
from kora.client.network import DirectTransport, SocketBytes
from kora.client.transport import Exchange, openapi_document, verified_context
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
        waiting.cancel()
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


class PausedConnectLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        super().__init__()
        self.connected = asyncio.Event()
        self.resume_handoff = asyncio.Event()

    async def sock_connect(
        self,
        sock: socket.socket,
        address: str | Buffer | tuple[object, ...] | int,
    ) -> None:
        if isinstance(address, int):
            raise TypeError("The loopback probe requires an IP socket address.")
        await super().sock_connect(sock, address)
        self.connected.set()
        await self.resume_handoff.wait()


def test_client_releases_connected_socket_before_pool_handoff() -> None:
    loop = PausedConnectLoop()
    accepted = asyncio.Event()
    received: list[bytes] = []

    async def handle(reader: Reader, _writer: Writer) -> None:
        accepted.set()
        received.append(await asyncio.wait_for(reader.read(), BOUND))

    async def exercise() -> bool:
        async with peer(handle) as origin, KoraClient(origin.url, CREDENTIAL) as client:
            request = asyncio.create_task(client.openapi())
            await asyncio.wait_for(loop.connected.wait(), BOUND)
            await asyncio.wait_for(accepted.wait(), BOUND)
            await client.aclose()
            with pytest.raises(asyncio.CancelledError):
                await request
        return request.done()

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        settled = runner.run(exercise())

    assert settled
    assert received == [b""]


@pytest.mark.asyncio
async def test_client_reclaims_pool_at_http_handoff() -> None:
    eof = asyncio.Event()
    accepted = 0

    async def handle(reader: Reader, writer: Writer) -> None:
        nonlocal accepted
        accepted += 1
        if accepted == 1:
            assert await asyncio.wait_for(reader.read(), BOUND) == b""
            eof.set()
        else:
            await read_request(reader)
            reply(writer)

    exchange = Exchange(tls=False)

    async def cancel_at_handoff(event: str, values: dict[str, object]) -> None:
        await exchange.observe(event, values)
        if event == "connection.connect_tcp.complete":
            exchange.scope.cancel()

    async with (
        peer(handle) as origin,
        DirectTransport(verified_context(None), 1) as transport,
    ):
        async with asyncio.timeout(BOUND):
            with exchange.scope, exchange.handoff:
                await transport.handle_async_request(
                    httpx.Request(
                        "GET", origin.url, extensions={"trace": cancel_at_handoff}
                    )
                )
            await eof.wait()
            response = await transport.handle_async_request(
                httpx.Request("GET", origin.url)
            )
            try:
                content = await response.aread()
            finally:
                await response.aclose()

    assert exchange.scope.cancelled_caught
    assert accepted == 2
    assert b'"openapi":"3.1.0"' in content


class DeadlineLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        super().__init__()
        self.advance: float = 0.0

    def time(self) -> float:
        return super().time() + self.advance


def test_client_applies_deadline_to_validation_failure() -> None:
    loop = DeadlineLoop()

    async def handle(reader: Reader, writer: Writer) -> None:
        await read_request(reader)
        reply(writer, b"{}")

    def advance_clock() -> None:
        loop.advance = 6.0

    def advance_before_waiter_resumes() -> None:
        loop.call_soon(advance_clock)

    def decode(document: JsonObject) -> JsonObject:
        loop.call_soon(advance_before_waiter_resumes)
        return openapi_document(document)

    async def exercise() -> ClientError:
        async with (
            peer(handle) as origin,
            ProbeClient(origin.url, CREDENTIAL, request_timeout=5.0) as client,
        ):
            with pytest.raises(ClientError) as refusal:
                await client.probe("GET", response_limit=1024, decode=decode)
            return refusal.value

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        result = runner.run(exercise())

    assert loop.advance == 6.0
    assert result.reason is Failure.DEADLINE
    assert result.outcome == "unknown"


@pytest.mark.asyncio
async def test_socket_read_delivers_cancellation_with_buffered_bytes() -> None:
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def handle(reader: Reader, writer: Writer) -> None:
        writer.write(b"x")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), BOUND) == b""

    async with peer(handle) as origin:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.setblocking(False)
            await loop.sock_connect(connection, ("127.0.0.1", origin.port))
            loop.add_reader(connection, ready.set)
            try:
                await asyncio.wait_for(ready.wait(), BOUND)
            finally:
                loop.remove_reader(connection)
            stream = SocketBytes(connection)

            with CancelScope() as cancellation:
                loop.call_soon(cancellation.cancel)
                await stream.receive(1)
            remaining = await asyncio.wait_for(stream.receive(1), BOUND)

    assert cancellation.cancelled_caught
    assert remaining == b"x"
