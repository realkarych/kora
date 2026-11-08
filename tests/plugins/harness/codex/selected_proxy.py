import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

MAX_PROXY_CONNECTIONS = 16
MAX_PROXY_HEADERS = 16_384
MAX_TUNNEL_BYTES = 1_048_576


class SelectedProxyError(ValueError):
    pass


class ModelRoute(StrEnum):
    DIRECT_HTTP = "direct-http"
    PROXY_HTTP = "proxy-http"
    DIRECT_TLS = "direct-tls"
    PROXY_TLS = "proxy-tls"


@dataclass(frozen=True)
class SelectedProxyCase:
    route: ModelRoute
    name: str | None = None
    bypass: str | None = None

    @property
    def tls(self) -> bool:
        return self.route in {ModelRoute.DIRECT_TLS, ModelRoute.PROXY_TLS}

    def environment(self, port: int) -> dict[str, str]:
        values: dict[str, str] = {}
        if self.name is not None:
            values[self.name] = f"http://127.0.0.1:{port}"
        if self.bypass is not None:
            values[self.bypass] = "127.0.0.1"
        return values


SELECTED_PROXY_CASES: dict[str, SelectedProxyCase] = {
    "http-control": SelectedProxyCase(ModelRoute.DIRECT_HTTP),
    "upper-http": SelectedProxyCase(ModelRoute.PROXY_HTTP, "HTTP_PROXY"),
    "lower-http": SelectedProxyCase(ModelRoute.PROXY_HTTP, "http_proxy"),
    "upper-all": SelectedProxyCase(ModelRoute.PROXY_HTTP, "ALL_PROXY"),
    "lower-all": SelectedProxyCase(ModelRoute.PROXY_HTTP, "all_proxy"),
    "upper-bypass": SelectedProxyCase(ModelRoute.DIRECT_HTTP, "HTTP_PROXY", "NO_PROXY"),
    "lower-bypass": SelectedProxyCase(ModelRoute.DIRECT_HTTP, "http_proxy", "no_proxy"),
    "https-control": SelectedProxyCase(ModelRoute.DIRECT_TLS),
    "upper-https": SelectedProxyCase(ModelRoute.PROXY_TLS, "HTTPS_PROXY"),
    "lower-https": SelectedProxyCase(ModelRoute.PROXY_TLS, "https_proxy"),
}
PROXY_NAMES = frozenset(
    name
    for case in SELECTED_PROXY_CASES.values()
    for name in (case.name, case.bypass)
    if name is not None
)
ProxyRequest = tuple[bytes, bytes]
ModelReceiver = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter, ProxyRequest], None
]


def loopback_port(address: object) -> int:
    if isinstance(address, tuple):
        fields = cast("tuple[object, ...]", address)
        if len(fields) == 2 and fields[0] == "127.0.0.1" and type(fields[1]) is int:
            return fields[1]
    raise SelectedProxyError("selected_proxy_unexpected_socket")


async def copy_tunnel(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    size = 0
    while data := await reader.read(4096):
        size += len(data)
        if size > MAX_TUNNEL_BYTES:
            raise SelectedProxyError("selected_proxy_tunnel_capacity")
        writer.write(data)
        await writer.drain()
    writer.write_eof()


class SelectedProxy:
    def __init__(
        self,
        case: SelectedProxyCase,
        origin: int,
        deadline: float,
        synthetic_key: bytes,
        receive_model: ModelReceiver,
    ) -> None:
        self._case: SelectedProxyCase = case
        self._origin: int = origin
        self._deadline: float = deadline
        self._synthetic_key: bytes = synthetic_key
        self._receive_model: ModelReceiver = receive_model
        self._server: asyncio.Server | None = None
        self._clients: dict[asyncio.StreamWriter, asyncio.Task[None]] = {}
        self._delegated: set[asyncio.StreamWriter] = set()
        self._overflow: asyncio.StreamWriter | None = None
        self._tunnel_ports: set[int] = set()
        self._direct: int = 0
        self._http: int = 0
        self._connect: int = 0
        self._tunneled: int = 0
        self.refused_connects: int = 0

    @property
    def port(self) -> int:
        server = self._server
        if server is None or not server.sockets:
            raise SelectedProxyError("selected_proxy_not_listening")
        return loopback_port(server.sockets[0].getsockname())

    async def start(self) -> None:
        try:
            async with asyncio.timeout_at(self._deadline):
                self._server = await asyncio.start_server(
                    self._accept,
                    "127.0.0.1",
                    0,
                    limit=MAX_PROXY_HEADERS,
                    backlog=MAX_PROXY_CONNECTIONS,
                )
        except (OSError, TimeoutError):
            raise SelectedProxyError("selected_proxy_listen_failed") from None

    def observe_origin(self, writer: asyncio.StreamWriter) -> None:
        port = loopback_port(writer.get_extra_info("peername"))
        if port in self._tunnel_ports:
            self._tunnel_ports.remove(port)
            self._tunneled += 1
        else:
            self._direct += 1

    def verify(self, requests: int) -> None:
        counts = {
            ModelRoute.DIRECT_HTTP: (requests, 0, 0, 0),
            ModelRoute.PROXY_HTTP: (0, requests, 0, 0),
            ModelRoute.DIRECT_TLS: (requests, 0, 0, 0),
            ModelRoute.PROXY_TLS: (0, 0, requests, requests),
        }
        if (self._direct, self._http, self._connect, self._tunneled) != counts[
            self._case.route
        ]:
            raise SelectedProxyError("selected_proxy_unexpected_model_route")

    def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if len(self._clients) >= MAX_PROXY_CONNECTIONS:
            writer.close()
            if self._overflow is None:
                self._overflow = writer
            if self._server is not None:
                self._server.close()
            return
        self._clients[writer] = asyncio.create_task(self._handle(reader, writer))

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            async with asyncio.timeout_at(self._deadline):
                headers = await reader.readuntil(b"\r\n\r\n")
                if len(headers) > MAX_PROXY_HEADERS:
                    raise SelectedProxyError("selected_proxy_header_capacity")
                line = headers.partition(b"\r\n")[0]
                origin = f"http://127.0.0.1:{self._origin}"
                route = f"POST {origin}/v1/responses HTTP/1.1".encode()
                if not self._case.tls and line == route:
                    self._receive_model(reader, writer, (route, headers))
                    self._delegated.add(writer)
                    self._http += 1
                    return
                if not line.startswith(b"CONNECT "):
                    raise SelectedProxyError("selected_proxy_unexpected_request")
                if self._synthetic_key in headers:
                    raise SelectedProxyError("selected_proxy_key_in_connect")
                authority = f"CONNECT 127.0.0.1:{self._origin} HTTP/1.1".encode()
                if self._case.tls and line == authority:
                    self._connect += 1
                    await self._tunnel(reader, writer)
                else:
                    self.refused_connects += 1
                    writer.write(
                        b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n"
                        b"Content-Length: 0\r\n\r\n"
                    )
                    await writer.drain()
        except (
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            raise SelectedProxyError("selected_proxy_invalid_stream") from None
        finally:
            if writer not in self._delegated:
                writer.close()

    async def _tunnel(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        upstream_reader, upstream = await asyncio.open_connection(
            "127.0.0.1", self._origin, limit=MAX_PROXY_HEADERS
        )
        port: int | None = None
        try:
            port = loopback_port(upstream.get_extra_info("sockname"))
            self._tunnel_ports.add(port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            try:
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(copy_tunnel(reader, upstream))
                    tasks.create_task(copy_tunnel(upstream_reader, writer))
            except* (OSError, SelectedProxyError):
                raise SelectedProxyError("selected_proxy_tunnel_failed") from None
        finally:
            if port is not None:
                self._tunnel_ports.discard(port)
            upstream.close()
            await upstream.wait_closed()

    async def close(self, deadline: float) -> None:
        server = self._server
        if server is None:
            return
        server.close()
        owned = self._clients.keys() - self._delegated
        for writer in owned:
            writer.close()
            self._clients[writer].cancel()
        if self._overflow is not None:
            owned.add(self._overflow)
        try:
            async with asyncio.timeout_at(deadline):
                outcomes = await asyncio.gather(
                    *self._clients.values(),
                    *(writer.wait_closed() for writer in owned),
                    server.wait_closed(),
                    return_exceptions=True,
                )
        except TimeoutError:
            raise SelectedProxyError("selected_proxy_cleanup_deadline") from None
        for outcome in outcomes:
            if isinstance(outcome, OSError):
                raise SelectedProxyError("selected_proxy_cleanup_failed") from None
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, asyncio.CancelledError
            ):
                raise outcome
        if self._overflow is not None:
            raise SelectedProxyError("selected_proxy_connection_capacity")
