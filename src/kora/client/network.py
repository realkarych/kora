import asyncio
import select
import socket
import ssl
from collections.abc import AsyncIterator, Iterable
from contextlib import ExitStack

import httpcore
import httpx
from anyio import BrokenResourceError, EndOfStream
from anyio.abc import ByteStream
from anyio.lowlevel import checkpoint
from anyio.streams.tls import TLSStream


class SocketBytes(ByteStream):
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection

    async def receive(self, max_bytes: int = 65536) -> bytes:
        await checkpoint()
        data = await asyncio.get_running_loop().sock_recv(self.connection, max_bytes)
        if not data:
            raise EndOfStream
        return data

    async def send(self, item: bytes) -> None:
        await checkpoint()
        await asyncio.get_running_loop().sock_sendall(self.connection, item)

    async def send_eof(self) -> None:
        self.connection.shutdown(socket.SHUT_WR)

    async def aclose(self) -> None:
        self.connection.close()


class DirectStream(httpcore.AsyncNetworkStream):
    def __init__(self, connection: socket.socket) -> None:
        self._tcp = SocketBytes(connection)
        self._stream: ByteStream = self._tcp

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            return await self._stream.receive(max_bytes)
        except EndOfStream:
            return b""
        except (BrokenResourceError, OSError):
            raise httpcore.ReadError("The response connection failed.") from None

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        try:
            await self._stream.send(buffer)
        except (BrokenResourceError, OSError):
            raise httpcore.WriteError("The request connection failed.") from None

    async def aclose(self) -> None:
        await self._tcp.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> "DirectStream":
        try:
            if not server_hostname:
                raise RuntimeError("TLS requires a verified hostname.")
            self._stream = await TLSStream.wrap(
                self._tcp,
                server_side=False,
                hostname=server_hostname,
                ssl_context=ssl_context,
                standard_compatible=False,
            )
        except (BrokenResourceError, EndOfStream, OSError):
            raise httpcore.ConnectError("TLS negotiation failed.") from None
        finally:
            if self._stream is self._tcp:
                await self.aclose()
        return self

    def get_extra_info(self, info: str) -> object:
        if info == "is_readable":
            descriptor = self._tcp.connection.fileno()
            if descriptor < 0:
                return True
            poll = select.poll()
            poll.register(descriptor, select.POLLIN)
            return bool(poll.poll(0))
        return None


class DirectBackend(httpcore.AsyncNetworkBackend):
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> DirectStream:
        if timeout is not None or local_address is not None or socket_options:
            raise RuntimeError(
                "The client requires direct TCP and one request deadline."
            )
        loop = asyncio.get_running_loop()
        if host == "127.0.0.1":
            addresses = [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (host, port),
                )
            ]
        else:
            try:
                addresses = await loop.getaddrinfo(
                    host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
                )
            except OSError:
                raise httpcore.ConnectError(
                    "The origin could not be resolved."
                ) from None
        for family, kind, protocol, _, address in addresses:
            try:
                with ExitStack() as ownership:
                    connection = ownership.enter_context(
                        socket.socket(family, kind, protocol)
                    )
                    connection.setblocking(False)
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    await checkpoint()
                    await loop.sock_connect(connection, address)
                    stream = DirectStream(connection)
                    ownership.pop_all()
                    return stream
            except OSError:
                continue
        raise httpcore.ConnectError("The origin connection failed.")


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, response: httpcore.Response) -> None:
        self._response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for part in self._response.aiter_stream():
            yield part

    async def aclose(self) -> None:
        await self._response.aclose()


class DirectTransport(httpx.AsyncBaseTransport):
    def __init__(self, tls: ssl.SSLContext, max_connections: int) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=tls,
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
            network_backend=DirectBackend(),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise TypeError("An asynchronous request body is required.")
        response = await self._pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            response.status, headers=response.headers, stream=ResponseStream(response)
        )

    async def aclose(self) -> None:
        await self._pool.aclose()
