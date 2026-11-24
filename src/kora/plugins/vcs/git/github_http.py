import asyncio
import ssl
from collections.abc import AsyncGenerator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast
from urllib.parse import unquote_to_bytes

import httpcore
import httpx

from kora.ir.json import JsonObject, JsonValue
from kora.ir.vcs_ref_json import (
    MAX_JSON_DEPTH,
    JsonRefusal,
    canonical_bytes,
    decode_strict_json,
)

MAX_RESPONSE_BYTES: int = 1_048_576
MAX_RESPONSE_HEADER_BYTES: int = 32_768
MAX_EXCHANGE_BYTES: int = 4_194_304


class GitHubTransportError(Exception):
    pass


@dataclass(frozen=True)
class HostResponse:
    status: int
    value: JsonValue | None
    has_link: bool = False


class ExchangeBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self._backend = cast("httpcore.AsyncNetworkBackend", httpcore.AnyIOBackend())
        self._stream: httpcore.AsyncNetworkStream | None = None

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._backend.connect_tcp(
            host, port, timeout, local_address, socket_options
        )
        self._stream = stream
        return stream

    async def close_exchange(self, pool: httpcore.AsyncConnectionPool) -> None:
        try:
            await pool.aclose()
        finally:
            if self._stream is not None:
                await self._stream.aclose()


class GitHubExchange:
    def __init__(
        self, origin: httpx.URL, pool: httpcore.AsyncConnectionPool, token: str
    ) -> None:
        self._origin = origin
        self._pool = pool
        self._token: str | None = token
        self._headers = (
            ("Accept", "application/vnd.github+json"),
            ("Accept-Encoding", "identity"),
            ("Host", origin.netloc),
            ("User-Agent", "kora"),
        )
        self._remaining = MAX_EXCHANGE_BYTES
        self.acknowledged_create = False

    def retire(self) -> None:
        self._token = None

    async def get(
        self, path: str, query: Mapping[str, str] | None = None
    ) -> HostResponse:
        return await self._request("GET", path, query, None)

    async def post(self, path: str, body: JsonObject) -> HostResponse:
        return await self._request("POST", path, None, canonical_bytes(body))

    def _url(self, path: str, query: Mapping[str, str] | None) -> str:
        if (
            not path.isascii()
            or not path.startswith("/")
            or any(char in path for char in "\\?#")
        ):
            raise GitHubTransportError
        raw_path = path.encode("ascii")
        decoded = unquote_to_bytes(raw_path)
        if (
            decoded.startswith(b"//")
            or b"\\" in decoded
            or any(segment in (b".", b"..") for segment in decoded.split(b"/"))
        ):
            raise GitHubTransportError
        target = self._origin.copy_with(raw_path=raw_path)
        if target.raw_path != raw_path:
            raise GitHubTransportError
        return str(target.copy_merge_params(query or {}))

    def _charge(self, size: int) -> bool:
        remaining = self._remaining - size
        self._remaining = max(remaining, 0)
        return remaining >= 0

    async def _request(
        self,
        method: str,
        path: str,
        query: Mapping[str, str] | None,
        body: bytes | None,
    ) -> HostResponse:
        if self._token is None or self._remaining == 0:
            raise GitHubTransportError
        headers = (*self._headers, ("Authorization", f"Bearer {self._token}"))
        if body is not None:
            headers = (*headers, ("Content-Type", "application/json"))
        try:
            async with self._pool.stream(
                method, self._url(path, query), headers=headers, content=body
            ) as response:
                self.acknowledged_create = method == "POST" and response.status == 201
                return await self._read(response, method == "GET")
        except (
            httpcore.NetworkError,
            httpcore.ProtocolError,
            httpx.InvalidURL,
            OSError,
        ):
            if self.acknowledged_create:
                return HostResponse(201, None)
            raise GitHubTransportError from None

    async def _read(self, response: httpcore.Response, get: bool) -> HostResponse:
        status = response.status
        refused = HostResponse(status, None)
        header_bytes = sum(len(key) + len(value) + 4 for key, value in response.headers)
        if not self._charge(header_bytes) or header_bytes > MAX_RESPONSE_HEADER_BYTES:
            return refused
        if status != (200 if get else 201):
            return refused
        headers = httpx.Headers(response.headers)
        encodings = headers.get_list("content-encoding")
        if encodings and [value.lower() for value in encodings] != ["identity"]:
            return refused
        payload = bytearray()
        async for chunk in response.aiter_stream():
            if (
                not self._charge(len(chunk))
                or len(payload) + len(chunk) > MAX_RESPONSE_BYTES
            ):
                return refused
            payload.extend(chunk)
        value = decode_strict_json(payload, MAX_RESPONSE_BYTES, MAX_JSON_DEPTH)
        if isinstance(value, JsonRefusal):
            return refused
        return HostResponse(status, value, get and "link" in headers)


@asynccontextmanager
async def github_exchange(
    origin: str, tls: ssl.SSLContext, token: str
) -> AsyncGenerator[GitHubExchange]:
    try:
        url = httpx.URL(origin)
    except httpx.InvalidURL:
        raise GitHubTransportError from None
    if (
        url.scheme != "https"
        or not url.host
        or url.userinfo
        or url.raw_path != b"/"
        or url.fragment
        or tls.verify_mode != ssl.CERT_REQUIRED
        or not tls.check_hostname
        or not token.isascii()
        or not token.isprintable()
    ):
        raise GitHubTransportError
    backend = ExchangeBackend()
    pool = httpcore.AsyncConnectionPool(
        ssl_context=tls,
        max_connections=1,
        max_keepalive_connections=0,
        network_backend=backend,
        retries=0,
    )
    exchange = GitHubExchange(url, pool, token)
    try:
        yield exchange
    finally:
        exchange.retire()
        closing = asyncio.create_task(backend.close_exchange(pool))
        interrupted = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                interrupted = True
        closing.result()
        if interrupted:
            raise asyncio.CancelledError
