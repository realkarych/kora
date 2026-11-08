import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from tests.plugins.harness.codex.owned_loopback import (
    STREAM_LIMIT,
    OwnedLoopback,
    loopback_port,
)

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
    [asyncio.StreamReader, asyncio.StreamWriter, ProxyRequest], Awaitable[None]
]


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
        self._upstreams: list[asyncio.StreamWriter] = []
        self._closing: bool = False
        self._tunnel_ports: set[int] = set()
        self._direct: int = 0
        self._http: int = 0
        self._connect: int = 0
        self._tunneled: int = 0
        self.refused_connects: int = 0
        self._listener: OwnedLoopback = OwnedLoopback(self._handle, deadline)
        self.port: int = self._listener.port

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

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            async with asyncio.timeout_at(self._deadline):
                headers = await reader.readuntil(b"\r\n\r\n")
                if len(headers) > STREAM_LIMIT:
                    raise SelectedProxyError("selected_proxy_header_capacity")
                line = headers.partition(b"\r\n")[0]
                origin = f"http://127.0.0.1:{self._origin}"
                route = f"POST {origin}/v1/responses HTTP/1.1".encode()
                if not self._case.tls and line == route:
                    self._http += 1
                    await self._receive_model(reader, writer, (route, headers))
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
            if not self._closing:
                raise SelectedProxyError("selected_proxy_invalid_stream") from None

    async def _tunnel(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        upstream_reader, upstream = await asyncio.open_connection(
            "127.0.0.1", self._origin, limit=STREAM_LIMIT
        )
        self._upstreams.append(upstream)
        port: int | None = None
        try:
            if self._closing:
                return
            port = loopback_port(upstream.get_extra_info("sockname"))
            self._tunnel_ports.add(port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            try:
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(copy_tunnel(reader, upstream))
                    tasks.create_task(copy_tunnel(upstream_reader, writer))
            except* (OSError, SelectedProxyError) as errors:
                if not self._closing or errors.subgroup(SelectedProxyError) is not None:
                    raise SelectedProxyError("selected_proxy_tunnel_failed") from None
        finally:
            if port is not None:
                self._tunnel_ports.discard(port)
            upstream.close()
            await upstream.wait_closed()

    async def close(self, deadline: float) -> None:
        self._closing = True
        for upstream in self._upstreams:
            upstream.close()
        try:
            await self._listener.close(deadline)
        finally:
            try:
                async with asyncio.timeout_at(deadline):
                    outcomes = await asyncio.gather(
                        *(upstream.wait_closed() for upstream in self._upstreams),
                        return_exceptions=True,
                    )
            except TimeoutError:
                raise SelectedProxyError("selected_proxy_cleanup_deadline") from None
            if any(isinstance(outcome, BaseException) for outcome in outcomes):
                raise SelectedProxyError("selected_proxy_cleanup_failed")
