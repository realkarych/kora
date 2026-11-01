from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import cast

import structlog.contextvars
from opentelemetry.context import Context, attach, detach
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kora.observability import (
    CATALOG,
    ComponentLogger,
    EventSpec,
    FieldKind,
    Level,
    ProcessDiagnostics,
    bound_scope,
)

COMPONENT: str = CATALOG.register_component("api")
COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "api.request.completed",
        COMPONENT,
        Level.DEBUG,
        "an API request finished",
        {
            "route": FieldKind.STRING,
            "status": FieldKind.INTEGER,
            "code": FieldKind.STRING,
        },
    )
)
FAILED: EventSpec = CATALOG.register(
    EventSpec(
        "api.request.failed",
        COMPONENT,
        Level.ERROR,
        "an API request failed unexpectedly",
        {"route": FieldKind.STRING, "error": FieldKind.EXCEPTION},
    )
)
HTTP_SCOPE_FIELDS: tuple[str, ...] = (
    "type",
    "asgi",
    "http_version",
    "server",
    "client",
    "scheme",
    "method",
    "root_path",
    "path",
    "raw_path",
    "query_string",
    "headers",
    "state",
    "extensions",
)


@dataclass
class Exchange:
    request_id: str
    logger: ComponentLogger
    route: str = "unmatched"
    status: int = 0
    code: str = "none"
    failed: bool = False

    def failure(self, error: Exception) -> None:
        if not self.failed:
            self.failed = True
            self.logger.emit(FAILED, route=self.route, error=error)


CURRENT: ContextVar[Exchange | None] = ContextVar("kora_api_exchange", default=None)


def current_exchange() -> Exchange:
    exchange = CURRENT.get()
    if exchange is None:
        raise RuntimeError("An API exchange is required.")
    return exchange


def method_of(scope: Scope) -> str:
    value: object = scope.get("method")
    if not isinstance(value, str):
        raise ValueError("An HTTP method is required.")
    return value


def response_headers(
    message: Message, request_id: str, head: bool
) -> list[tuple[bytes, bytes]]:
    value: object = message.get("headers", [])
    if not isinstance(value, list | tuple):
        raise ValueError("Response headers must be pairs of bytes.")
    headers: list[tuple[bytes, bytes]] = []
    excluded = {b"x-request-id"}
    if head:
        excluded.update({b"content-length", b"transfer-encoding"})
    for item in cast("Sequence[object]", value):
        if not isinstance(item, list | tuple):
            raise ValueError("Response headers must be pairs of bytes.")
        pair = cast("Sequence[object]", item)
        if len(pair) != 2:
            raise ValueError("Response headers must be pairs of bytes.")
        name, content = pair
        if not isinstance(name, bytes) or not isinstance(content, bytes):
            raise ValueError("Response headers must be pairs of bytes.")
        if name.lower() not in excluded:
            headers.append((name, content))
    headers.append((b"x-request-id", request_id.encode("ascii")))
    return headers


class ApiExchange:
    def __init__(
        self,
        application: ASGIApp,
        diagnostics: ProcessDiagnostics,
        request_id: Callable[[], str],
    ) -> None:
        self.application = application
        self.diagnostics = diagnostics
        self.request_id = request_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return
        saved_context: dict[str, object] = structlog.contextvars.get_contextvars()
        telemetry = attach(Context())
        previous = CURRENT.set(None)
        structlog.contextvars.clear_contextvars()
        try:
            identity = self.request_id()
            with bound_scope(request_id=identity):
                exchange = Exchange(
                    identity,
                    self.diagnostics.logger(COMPONENT).bind(request_id=identity),
                )
                CURRENT.set(exchange)
                head = method_of(scope) == "HEAD"

                async def canonical_send(message: Message) -> None:
                    status: int | None = None
                    if message["type"] == "http.response.start":
                        value: object = message.get("status")
                        if type(value) is not int:
                            raise ValueError("An HTTP response status is required.")
                        status = value
                        message = {
                            **message,
                            "headers": response_headers(message, identity, head),
                        }
                    elif message["type"] == "http.response.body" and head:
                        message = {**message, "body": b""}
                    await send(message)
                    if status is not None:
                        exchange.status = status

                try:
                    projected: Scope = {
                        key: scope[key] for key in HTTP_SCOPE_FIELDS if key in scope
                    }
                    await self.application(projected, receive, canonical_send)
                finally:
                    exchange.logger.emit(
                        COMPLETED,
                        route=exchange.route,
                        status=exchange.status,
                        code=exchange.code,
                    )
        finally:
            CURRENT.reset(previous)
            structlog.contextvars.clear_contextvars()
            structlog.contextvars.bind_contextvars(**saved_context)
            detach(telemetry)
