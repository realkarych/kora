import asyncio
import socket
import ssl
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager

import pytest

from tests.plugins.harness.codex.owned_loopback import OwnedLoopback, loopback_port
from tests.plugins.harness.codex.selected_proxy import (
    ModelRoute,
    ProxyRequest,
    SelectedProxy,
    SelectedProxyCase,
)


async def close_stream(writer: asyncio.StreamWriter) -> None:
    writer.close()
    await writer.wait_closed()


@asynccontextmanager
async def connect_loopback(
    port: int,
) -> AsyncGenerator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        yield reader, writer
    finally:
        await close_stream(writer)


async def unexpected_model_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: ProxyRequest
) -> None:
    raise AssertionError("a CONNECT test sent a model request")


@pytest.mark.asyncio
async def test_selected_proxy_refuses_and_settles_interrupted_tunnel() -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    async with asyncio.timeout_at(deadline), AsyncExitStack() as resources:
        accepted: asyncio.Future[asyncio.StreamReader] = loop.create_future()
        other_contacted = asyncio.Event()

        def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            resources.push_async_callback(close_stream, writer)
            accepted.set_result(reader)

        def accept_other(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            resources.push_async_callback(close_stream, writer)
            other_contacted.set()

        origin = await resources.enter_async_context(
            await asyncio.start_server(accept, "127.0.0.1", 0)
        )
        other = await resources.enter_async_context(
            await asyncio.start_server(accept_other, "127.0.0.1", 0)
        )
        origin_port = loopback_port(origin.sockets[0].getsockname())
        other_port = loopback_port(other.sockets[0].getsockname())
        proxy = SelectedProxy(
            SelectedProxyCase(ModelRoute.PROXY_TLS),
            origin_port,
            deadline,
            b"synthetic-provider-key",
            unexpected_model_request,
        )
        try:
            async with connect_loopback(proxy.port) as (denied_reader, denied_writer):
                denied_writer.write(
                    f"CONNECT 127.0.0.1:{other_port} HTTP/1.1\r\n\r\n".encode()
                )
                await denied_writer.drain()
                denied = await denied_reader.read()
            contacted_after_denial = accepted.done()
            reader, writer = await resources.enter_async_context(
                connect_loopback(proxy.port)
            )
            writer.write(f"CONNECT 127.0.0.1:{origin_port} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            connected = await reader.readuntil(b"\r\n\r\n")
            upstream = await accepted
        finally:
            await proxy.close(deadline)
        client_eof, upstream_eof = await asyncio.gather(
            reader.read(1), upstream.read(1)
        )

        assert denied == (
            b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
        )
        assert not contacted_after_denial
        assert not other_contacted.is_set()
        assert connected == b"HTTP/1.1 200 Connection Established\r\n\r\n"
        assert proxy.refused_connects == 1
        assert client_eof == upstream_eof == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix", [b"", b"\x16\x03\x03\x00\x10\x01"], ids=["idle", "partial-record"]
)
async def test_owned_loopback_settles_socket_before_tls_handshake(
    prefix: bytes,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    called = asyncio.Event()

    async def receive(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        called.set()

    async with asyncio.timeout_at(deadline):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as peer:
            peer.setblocking(False)
            server = OwnedLoopback(
                receive, deadline, ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            )

            try:
                await loop.sock_connect(peer, ("127.0.0.1", server.port))
                await loop.sock_sendall(peer, prefix)
                await server.transport_started.wait()
            finally:
                await server.close(deadline)
            received: bytes | ConnectionResetError
            try:
                received = await loop.sock_recv(peer, 1)
            except ConnectionResetError as error:
                received = error

        assert received == b"" or (
            bool(prefix) and isinstance(received, ConnectionResetError)
        )
        assert server.settled_connection_count == 1
        assert not called.is_set()
