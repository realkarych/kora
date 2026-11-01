from collections.abc import Callable
from typing import Literal

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from kora.api.authentication import schema_authorization
from kora.api.errors import definitive_error
from kora.api.exchange import ApiExchange, current_exchange, method_of
from kora.api.responses import (
    error_response,
    framework_error,
    unexpected_error,
    validation_error,
)
from kora.api.routes import RECOGNIZED_METHODS, ActiveCatalog, RouteName
from kora.models.errors import ApiErrorCode
from kora.observability import ProcessDiagnostics
from kora.protocols.authentication import Authenticator
from kora.protocols.time import Clock


class Health(BaseModel):
    status: Literal["ok"]


class ApiDispatch:
    def __init__(
        self,
        app: ASGIApp,
        *,
        catalog: ActiveCatalog,
        authenticator: Authenticator,
        clock: Clock,
    ) -> None:
        self.application = app
        self.catalog = catalog
        self.authenticator = authenticator
        self.clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return
        exchange = current_exchange()
        matched = self.catalog.match(scope)
        if matched is not None:
            exchange.route = matched.name.value
        method = method_of(scope)
        if matched is not None and matched.name == RouteName.SCHEMA:
            refusal = await schema_authorization(
                Request(scope), self.authenticator, self.clock
            )
            if refusal is not None:
                await error_response(refusal, method=method)(scope, receive, send)
                return
        code: ApiErrorCode | None = None
        if method not in RECOGNIZED_METHODS:
            code = ApiErrorCode.METHOD_NOT_IMPLEMENTED
        elif matched is None:
            code = ApiErrorCode.NOT_FOUND
        elif method not in matched.methods:
            code = ApiErrorCode.METHOD_NOT_ALLOWED
        if code is not None:
            response = error_response(
                definitive_error(code, request_id=exchange.request_id),
                method=method,
                matched=matched,
            )
            await response(scope, receive, send)
            return
        await self.application(scope, receive, send)


def create_application(
    authenticator: Authenticator,
    diagnostics: ProcessDiagnostics,
    clock: Clock,
    request_id: Callable[[], str],
) -> ApiExchange:
    framework = FastAPI(
        title="kora",
        version="1",
        debug=False,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
        redirect_slashes=False,
        telemetry={
            "tracing": False,
            "metrics": False,
            "logs": False,
            "operation_spans": False,
            "auto_configure": False,
        },
    )

    async def health() -> Health:
        return Health(status="ok")

    async def schema() -> Response:
        return JSONResponse(framework.openapi())

    framework.add_api_route(
        RouteName.HEALTH.value,
        health,
        methods=["GET"],
        operation_id="health",
        response_model=Health,
    )
    framework.add_api_route(
        RouteName.SCHEMA.value, schema, methods=["GET"], include_in_schema=False
    )
    framework.add_exception_handler(HTTPException, framework_error)
    framework.add_exception_handler(RequestValidationError, validation_error)
    framework.add_exception_handler(Exception, unexpected_error)
    framework.add_middleware(
        ApiDispatch,
        catalog=ActiveCatalog(framework),
        authenticator=authenticator,
        clock=clock,
    )
    return ApiExchange(framework, diagnostics, request_id)
