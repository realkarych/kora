import asyncio
import json
import math
import re
import ssl
from collections.abc import Callable
from contextvars import Context
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self, TypeVar
from urllib.parse import quote, urlsplit

import certifi
import httpcore
import httpx
from anyio import CancelScope
from anyio.lowlevel import checkpoint_if_cancelled
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic_core import PydanticCustomError

from kora.client.asgi import (
    ASGIApp,
    AsgiResponseIncomplete,
    AsgiResponseLimit,
    InProcessTransport,
)
from kora.client.documents import NAMESPACE
from kora.client.errors import (
    ApiError,
    ApiErrorFields,
    ClientError,
    Correlation,
    Failure,
    Outcome,
)
from kora.client.network import DirectTransport
from kora.ir.json import JsonObject, JsonShapeError, parse_json_object
from kora.models.errors import HTTP_STATUSES, ApiErrorCode, require_api_error_fields
from kora.models.graphs import DeployRequest, DeployResponse
from kora.models.runs import AcceptedRun, SkippedRun, StartOutcome, TriggerRequest
from kora.models.workers import RegisteredWorker, WorkerRegistration
from kora.observability import CATALOG, EventSpec, FieldKind, Level, ProcessDiagnostics

OPENAPI_PATH: str = "/api/v1/openapi.json"
OPENAPI_RESPONSE_BYTES: int = 4 * 1024 * 1024
GRAPH_VERSIONS_PATH: str = "/api/v1/namespaces/{ns}/graphs/{graph}/versions"
DEPLOY_RESPONSE_BYTES: int = 2 * 1024 * 1024
GRAPH_RUNS_PATH: str = "/api/v1/namespaces/{ns}/graphs/{graph}/runs"
IDEMPOTENCY_KEY = re.compile(r"[!-~]{1,255}")
OK: frozenset[int] = frozenset({200})
CREATED_OR_OK: frozenset[int] = frozenset({200, 201})
JSON_DEPTH: int = 64
HTTP_LOOPBACK: re.Pattern[str] = re.compile(r"127\.0\.0\.1(?::[0-9]+)?")
TOKEN: re.Pattern[str] = re.compile(r"[a-zA-Z0-9._~+/\-]+=*")
REQUEST_ID: re.Pattern[str] = re.compile(r"req_[a-zA-Z0-9_-]{1,252}")
API_STATUSES: dict[str, int] = {
    code.value: status for code, status in HTTP_STATUSES.items()
}
T = TypeVar("T")

COMPONENT: str = CATALOG.register_component("client")
COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "client.request.completed",
        COMPONENT,
        Level.DEBUG,
        "an API client request finished",
        {
            "outcome": FieldKind.STRING,
            "code": FieldKind.STRING,
            "status": FieldKind.INTEGER,
        },
    )
)


class OpenAPIInfo(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    title: str = Field(min_length=1)
    version: str = Field(min_length=1)


class OpenAPIFields(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    openapi: str = Field(pattern=r"^3\.[0-9]+\.[0-9]+$")
    info: OpenAPIInfo
    paths: dict[str, object]


def openapi_document(value: JsonObject) -> JsonObject:
    OpenAPIFields.model_validate(value)
    return value


@dataclass(frozen=True, slots=True)
class DeployedGraphVersion:
    response: DeployResponse
    document: JsonObject


def deployed_graph_version(value: JsonObject) -> DeployedGraphVersion:
    return DeployedGraphVersion(DeployResponse.model_validate(value), value)


@dataclass(frozen=True, slots=True)
class TriggeredRun:
    response: AcceptedRun | SkippedRun
    document: JsonObject


START_OUTCOME: TypeAdapter[AcceptedRun | SkippedRun] = TypeAdapter(StartOutcome)


def triggered_run(value: JsonObject) -> TriggeredRun:
    return TriggeredRun(START_OUTCOME.validate_python(value), value)


def registered_worker(value: JsonObject) -> RegisteredWorker:
    return RegisteredWorker.model_validate(
        {key: value[key] for key in RegisteredWorker.model_fields if key in value}
    )


def direct_origin(value: str) -> httpx.URL:
    try:
        if not value.isascii() or any(
            c.isspace() or not c.isprintable() for c in value
        ):
            raise ValueError
        if any(c in value for c in "\\?#"):
            raise ValueError
        parts = urlsplit(value)
        if (
            not parts.hostname
            or "@" in parts.netloc
            or parts.path not in ("", "/")
            or parts.port == 0
            or parts.scheme not in ("http", "https")
            or parts.scheme == "http"
            and not HTTP_LOOPBACK.fullmatch(parts.netloc)
        ):
            raise ValueError
        return httpx.URL(value)
    except (ValueError, httpx.InvalidURL):
        raise ClientError(Failure.DESTINATION_POLICY, Outcome.NOT_SENT) from None


def verified_context(bundle: Path | None) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        context.load_verify_locations(
            cafile=bundle if bundle is not None else certifi.where()
        )
    except OSError:
        raise ClientError(Failure.TLS, Outcome.NOT_SENT) from None
    return context


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result


def reject_constant(_value: str) -> None:
    raise ValueError


async def settle(future: asyncio.Future[T]) -> T:
    interrupted = False
    while True:
        try:
            result = await asyncio.shield(future)
            break
        except asyncio.CancelledError:
            if future.cancelled():
                raise
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError
    return result


@dataclass
class Exchange:
    tls: bool
    scope: CancelScope = field(default_factory=CancelScope)
    handoff: CancelScope = field(default_factory=CancelScope)
    request_id: str | None = None
    status: int | None = None
    tls_failed: bool = False
    sending: bool = False

    def mark_sending(self) -> None:
        self.sending = True

    def failure(self, reason: Failure) -> ClientError:
        outcome = Outcome.UNKNOWN if self.sending else Outcome.NOT_SENT
        return ClientError(reason, outcome, Correlation(self.request_id))

    async def observe(self, event: str, values: dict[str, object]) -> None:
        match event:
            case "connection.connect_tcp.complete" if not self.tls:
                self.handoff.shield = True
            case "connection.start_tls.complete":
                self.handoff.shield = True
            case "connection.start_tls.failed":
                self.tls_failed = True
            case "http11.send_request_headers.started":
                self.handoff.shield = False
                await checkpoint_if_cancelled()
                self.sending = True
            case _:
                pass
        if event.endswith((".complete", ".failed")):
            values.clear()


class KoraClient:
    def __init__(
        self,
        origin: str,
        token: str,
        *,
        request_timeout: float = 15.0,
        max_connections: int = 10,
        ca_bundle: Path | None = None,
        proxy: str | None = None,
        diagnostics: ProcessDiagnostics | None = None,
        asgi_app: ASGIApp | None = None,
    ) -> None:
        if proxy is not None:
            raise ClientError(Failure.PROXY_POLICY, Outcome.NOT_SENT)
        self._origin = direct_origin(origin)
        if not TOKEN.fullmatch(token):
            raise ValueError("A valid bearer token is required.")
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("The request timeout must be positive and finite.")
        if type(max_connections) is not int or max_connections <= 0:
            raise ValueError("The connection limit must be a positive integer.")
        self._token = token
        self._timeout = request_timeout
        self._logger = (
            diagnostics.logger(COMPONENT) if diagnostics is not None else None
        )
        if asgi_app is not None and (
            origin != "http://127.0.0.1" or ca_bundle is not None
        ):
            raise ClientError(Failure.DESTINATION_POLICY, Outcome.NOT_SENT)
        self._transport = (
            DirectTransport(verified_context(ca_bundle), max_connections)
            if asgi_app is None
            else InProcessTransport(asgi_app, max_connections)
        )
        self._active: dict[asyncio.Task[object], CancelScope] = {}
        self._closing: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await settle(self._closing)

    async def _close(self) -> None:
        active = tuple(self._active.items())
        for _, scope in active:
            scope.cancel()
        await asyncio.gather(*(task for task, _ in active), return_exceptions=True)
        await self._transport.aclose()

    async def openapi(self) -> JsonObject:
        return await self._request_json(
            "GET", OPENAPI_PATH, openapi_document, response_limit=OPENAPI_RESPONSE_BYTES
        )

    async def deploy(
        self, namespace: str, request: DeployRequest
    ) -> DeployedGraphVersion:
        return await self._request_json(
            "POST",
            GRAPH_VERSIONS_PATH.format(
                ns=quote(namespace, safe=""), graph=quote(request.graph, safe="")
            ),
            deployed_graph_version,
            response_limit=DEPLOY_RESPONSE_BYTES,
            body=request.model_dump(mode="json"),
            success=CREATED_OR_OK,
        )

    async def trigger(
        self,
        namespace: str,
        graph: str,
        request: TriggerRequest,
        idempotency_key: str | None = None,
    ) -> TriggeredRun:
        if not all(NAMESPACE.fullmatch(name) for name in (namespace, graph)):
            raise ClientError(Failure.DESTINATION_POLICY, Outcome.NOT_SENT)
        return await self._request_json(
            "POST",
            GRAPH_RUNS_PATH.format(
                ns=quote(namespace, safe=""), graph=quote(graph, safe="")
            ),
            triggered_run,
            response_limit=DEPLOY_RESPONSE_BYTES,
            body=request.model_dump(mode="json", exclude_none=True),
            success=CREATED_OR_OK,
            headers=()
            if idempotency_key is None
            else (("Idempotency-Key", idempotency_key),),
        )

    async def register_worker(
        self, registration: WorkerRegistration
    ) -> RegisteredWorker:
        body = parse_json_object(registration.model_dump(mode="json"))
        return await self._request_json(
            "POST",
            "/api/v1/workers",
            registered_worker,
            response_limit=16 * 1024,
            body=body,
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        decode: Callable[[JsonObject], T],
        *,
        response_limit: int,
        body: JsonObject | None = None,
        success: frozenset[int] = OK,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> T:
        if self._closing is not None:
            raise RuntimeError("The API client is closed.")
        if (
            not path.startswith("/")
            or path.startswith("//")
            or any(char in path for char in "\\?#")
        ):
            raise ClientError(Failure.DESTINATION_POLICY, Outcome.NOT_SENT)
        exchange = Exchange(tls=self._origin.scheme == "https")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        task = asyncio.create_task(
            self._exchange(
                method,
                path,
                decode,
                response_limit,
                (body, headers),
                success,
                exchange,
            )
        )
        self._active[task] = exchange.scope
        try:
            try:
                async with asyncio.timeout_at(deadline):
                    try:
                        await asyncio.shield(task)
                    except (ApiError, ClientError):
                        if loop.time() < deadline:
                            raise
                    if loop.time() >= deadline:
                        raise exchange.failure(Failure.DEADLINE) from None
                    result = task.result()
            except TimeoutError:
                exchange.scope.cancel()
                await settle(asyncio.gather(task, return_exceptions=True))
                raise exchange.failure(Failure.DEADLINE) from None
        except asyncio.CancelledError:
            exchange.scope.cancel()
            await settle(asyncio.gather(task, return_exceptions=True))
            self._report("cancelled", Correlation(exchange.request_id))
            raise
        except ClientError as error:
            self._report("client_error", error.correlation, error.code)
            raise
        except ApiError as error:
            code = error.code if error.code in API_STATUSES else "additive"
            self._report("api_error", error.correlation, code, error.status)
            raise
        else:
            self._report("ok", Correlation(exchange.request_id), status=exchange.status)
            return result
        finally:
            del self._active[task]

    async def _exchange(
        self,
        method: str,
        path: str,
        decode: Callable[[JsonObject], T],
        response_limit: int,
        payload: tuple[JsonObject | None, tuple[tuple[str, str], ...]],
        success: frozenset[int],
        exchange: Exchange,
    ) -> T:
        body, headers = payload
        request = httpx.Request(
            method,
            self._origin.copy_with(path=path),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                **dict(headers),
            },
            json=body,
            extensions={"trace": exchange.observe},
        )
        with exchange.scope, exchange.handoff:
            status, content = await self._receive(
                request, response_limit, success, exchange
            )
            return self._decode_response(
                content, status, decode, response_limit, success, exchange
            )
        raise asyncio.CancelledError

    async def _receive(
        self,
        request: httpx.Request,
        response_limit: int,
        success: frozenset[int],
        exchange: Exchange,
    ) -> tuple[int, bytes]:
        try:
            if isinstance(self._transport, InProcessTransport):
                response = await self._transport.receive(
                    request, response_limit, exchange.mark_sending
                )
            else:
                response = await self._transport.handle_async_request(request)
            try:
                exchange.status = response.status_code
                ids = response.headers.get_list("X-Request-ID")
                exchange.request_id = self._safe_id(ids[0]) if len(ids) == 1 else None
                content = await read_body(response, response_limit, success, exchange)
                return response.status_code, content
            finally:
                with CancelScope(shield=True):
                    await response.aclose()
        except AsgiResponseLimit:
            raise exchange.failure(Failure.RESPONSE_LIMIT) from None
        except AsgiResponseIncomplete:
            raise exchange.failure(Failure.STREAM) from None
        except httpcore.ConnectError:
            reason = Failure.TLS if exchange.tls_failed else Failure.CONNECTION
            raise exchange.failure(reason) from None
        except httpcore.PoolTimeout:
            raise exchange.failure(Failure.POOL) from None
        except (httpcore.NetworkError, httpcore.ProtocolError):
            raise exchange.failure(Failure.STREAM) from None

    def _decode_response(
        self,
        content: bytes,
        status: int,
        decode: Callable[[JsonObject], T],
        response_limit: int,
        success: frozenset[int],
        exchange: Exchange,
    ) -> T:
        try:
            raw: object = json.loads(
                content.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
            )
        except (ValueError, RecursionError):
            reason = Failure.JSON if status in success else Failure.STATUS_ENVELOPE
            raise exchange.failure(reason) from None
        try:
            value = parse_json_object(
                raw, max_depth=JSON_DEPTH, max_bytes=response_limit
            )
        except JsonShapeError:
            reason = Failure.SCHEMA if status in success else Failure.STATUS_ENVELOPE
            raise exchange.failure(reason) from None
        if status >= 400:
            raise self._api_error(value, content, status, exchange)
        try:
            return decode(value)
        except ValidationError:
            raise exchange.failure(Failure.SCHEMA) from None

    def _api_error(
        self, value: JsonObject, content: bytes, status: int, exchange: Exchange
    ) -> ApiError:
        try:
            error = ApiErrorFields.model_validate(value.get("error"))
            expected_status = API_STATUSES.get(error.code)
            if expected_status is not None:
                if expected_status != status:
                    raise exchange.failure(Failure.STATUS_ENVELOPE)
                require_api_error_fields(
                    ApiErrorCode(error.code), error.message, error.retryable
                )
        except (ValidationError, PydanticCustomError):
            raise exchange.failure(Failure.STATUS_ENVELOPE) from None
        original = self._safe_id(error.request_id)
        correlation = Correlation(
            exchange.request_id,
            original
            if exchange.request_id is not None and original != exchange.request_id
            else None,
        )
        return ApiError(status, error.code, value, content, correlation)

    def _safe_id(self, value: str) -> str | None:
        if REQUEST_ID.fullmatch(value) and self._token not in value:
            return value
        return None

    def _report(
        self,
        outcome: str,
        correlation: Correlation,
        code: str | None = None,
        status: int | None = None,
    ) -> None:
        if self._logger is None:
            return
        context: dict[str, str] = {}
        fields: dict[str, object] = {"outcome": outcome}
        if correlation.request_id is not None:
            context["request_id"] = correlation.request_id
        if correlation.replayed_request_id is not None:
            context["replayed_request_id"] = correlation.replayed_request_id
        if code is not None:
            fields["code"] = code
        if status is not None:
            fields["status"] = status
        level = Level.WARNING if outcome == "client_error" else Level.DEBUG
        Context().run(self._logger.bind(**context).emit_at, level, COMPLETED, **fields)


async def read_body(
    response: httpx.Response, limit: int, success: frozenset[int], exchange: Exchange
) -> bytes:
    status = response.status_code
    if status not in success and not 400 <= status < 600:
        raise exchange.failure(Failure.STATUS_ENVELOPE)
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise exchange.failure(Failure.STATUS_ENVELOPE)
    content = bytearray()
    async for chunk in response.aiter_raw():
        if len(chunk) > limit - len(content):
            raise exchange.failure(Failure.RESPONSE_LIMIT)
        content.extend(chunk)
    return bytes(content)
