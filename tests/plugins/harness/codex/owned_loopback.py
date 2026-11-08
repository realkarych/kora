import asyncio
import errno
import socket
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

MAX_CONNECTIONS = 16
STREAM_LIMIT = 16_384
TLS_HANDSHAKE_SECONDS = 5
StreamHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


class LoopbackError(ValueError):
    pass


def loopback_port(address: object) -> int:
    if isinstance(address, tuple):
        fields = cast("tuple[object, ...]", address)
        if len(fields) == 2 and fields[0] == "127.0.0.1" and type(fields[1]) is int:
            return fields[1]
    raise LoopbackError("probe_unexpected_loopback_socket")


class TrackedSocket(socket.socket):
    def __init__(self, accepted: socket.socket) -> None:
        super().__init__(socket.AF_INET, socket.SOCK_STREAM, fileno=accepted.detach())
        self.closed: asyncio.Event = asyncio.Event()

    def close(self) -> None:
        super().close()
        if self.fileno() == -1:
            self.closed.set()


@dataclass
class AcceptedConnection:
    socket: TrackedSocket
    transport: asyncio.Transport | None = None

    def stop(self) -> None:
        if self.socket.closed.is_set():
            return
        if self.transport is not None:
            self.transport.abort()
            return
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError as error:
            if error.errno != errno.ENOTCONN:
                raise LoopbackError("probe_loopback_shutdown_failed") from None


class OwnedLoopback:
    def __init__(
        self,
        handler: StreamHandler,
        deadline: float,
        tls: ssl.SSLContext | None = None,
    ) -> None:
        self._loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        self._handler: StreamHandler = handler
        self._deadline: float = deadline
        self._tls: ssl.SSLContext | None = tls
        self._connections: dict[asyncio.Task[None], AcceptedConnection] = {}
        self._closing: bool = False
        self._failure: str | None = None
        self.transport_started: asyncio.Event = asyncio.Event()
        self._listener: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM
        )
        try:
            self._listener.setblocking(False)
            self._listener.bind(("127.0.0.1", 0))
            self._listener.listen(MAX_CONNECTIONS)
            self.port: int = loopback_port(self._listener.getsockname())
            self._loop.add_reader(self._listener.fileno(), self._accept)
        except (OSError, ValueError):
            self._listener.close()
            raise LoopbackError("probe_loopback_listen_failed") from None

    def _fence(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._loop.remove_reader(self._listener.fileno())
        self._listener.close()

    def _accept(self) -> None:
        if self._closing:
            return
        try:
            incoming, _ = self._listener.accept()
        except BlockingIOError:
            return
        except OSError:
            self._failure = "probe_loopback_accept_failed"
            self._fence()
            return
        if len(self._connections) >= MAX_CONNECTIONS:
            incoming.close()
            self._failure = "probe_loopback_connection_capacity"
            self._fence()
            return
        connection = AcceptedConnection(TrackedSocket(incoming))
        self._connections[self._loop.create_task(self._serve(connection))] = connection

    async def _serve(self, connection: AcceptedConnection) -> None:
        incoming = connection.socket
        if self._closing:
            incoming.close()
            return
        reader = asyncio.StreamReader(limit=STREAM_LIMIT)
        protocol = asyncio.StreamReaderProtocol(reader)

        def stream_protocol() -> asyncio.StreamReaderProtocol:
            self.transport_started.set()
            return protocol

        try:
            remaining = self._deadline - self._loop.time()
            if remaining <= 0:
                incoming.close()
                raise LoopbackError("probe_loopback_deadline")
            transport, _ = await self._loop.connect_accepted_socket(
                stream_protocol,
                incoming,
                ssl=self._tls,
                ssl_handshake_timeout=(
                    min(remaining, TLS_HANDSHAKE_SECONDS)
                    if self._tls is not None
                    else None
                ),
            )
            connection.transport = transport
            if self._closing:
                transport.abort()
            else:
                writer = asyncio.StreamWriter(transport, protocol, reader, self._loop)
                await self._handler(reader, writer)
        except (OSError, TimeoutError):
            if not self._closing:
                raise LoopbackError("probe_loopback_connection_failed") from None
        finally:
            if connection.transport is not None:
                connection.transport.close()
            await incoming.closed.wait()

    async def close(self, deadline: float) -> None:
        self._fence()
        failure = self._failure
        for connection in self._connections.values():
            try:
                connection.stop()
            except LoopbackError as error:
                if failure is None:
                    failure = str(error)
        if self._connections:
            _, pending = await asyncio.wait(
                self._connections,
                timeout=max(0, deadline - self._loop.time()),
            )
            if pending:
                for task in pending:
                    self._connections[task].socket.close()
                    task.cancel()
                raise LoopbackError("probe_loopback_cleanup_deadline")
            outcomes = await asyncio.gather(*self._connections, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
        if failure is not None:
            raise LoopbackError(failure)
