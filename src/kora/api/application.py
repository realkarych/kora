from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kora.api.authentication import authenticate_route
from kora.api.errors import RequestRejection, definitive_error, request_rejection
from kora.api.exchange import ApiExchange, current_exchange, method_of
from kora.api.graph_deployment_route import deployment_handler
from kora.api.graph_services import GraphServices
from kora.api.responses import (
    error_response,
    framework_error,
    unexpected_error,
    validation_error,
)
from kora.api.routes import (
    PROTECTED_ROUTES,
    RECOGNIZED_METHODS,
    ActiveCatalog,
    RouteName,
)
from kora.api.run_trigger_route import trigger_handler
from kora.api.worker_claims_route import WorkerClaimsRoute
from kora.api.worker_history_route import WorkerHistoryRoute
from kora.api.worker_registration_route import WorkerRegistrationRoute
from kora.api.worker_usage_route import WorkerUsageRoute
from kora.engine.admission_initialization import AdmissionInitialization
from kora.models.errors import ApiErrorCode, ApiErrorEnvelope
from kora.models.graphs import DeployRequest, DeployResponse
from kora.models.runs import AcceptedRun, SkippedRun, TriggerRequest
from kora.models.worker_claims import WorkerClaimRequest, WorkerClaimResponse
from kora.models.worker_history import WorkerHistoryQuery
from kora.models.worker_usage import AcceptedUsageReport, UsageReport
from kora.models.workers import RegisteredWorker, WorkerName, WorkerRegistration
from kora.observability import ProcessDiagnostics
from kora.protocols.authentication import Authenticator
from kora.protocols.history import GrantHistoryPage
from kora.protocols.identity import Identifier
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
        worker_history: WorkerHistoryRoute | None = None,
    ) -> None:
        self.application = app
        self.catalog = catalog
        self.authenticator = authenticator
        self.clock = clock
        self.worker_history = worker_history

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return
        exchange = current_exchange()
        matched = self.catalog.match(scope)
        if matched is not None:
            exchange.route = matched.name.value
        method = method_of(scope)
        if matched is not None and matched.name in PROTECTED_ROUTES:
            authority = await authenticate_route(
                Request(scope),
                self.authenticator,
                self.clock,
                "service"
                if matched.name
                in (
                    RouteName.WORKERS,
                    RouteName.WORKER_CLAIMS,
                    RouteName.WORKER_HISTORY,
                    RouteName.USAGE,
                )
                else "user",
                current_authority=matched.name is RouteName.WORKER_CLAIMS,
            )
            if isinstance(authority, ApiErrorEnvelope):
                await error_response(authority, method=method)(scope, receive, send)
                return
            exchange.principal = authority
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
        if matched is not None and matched.name is RouteName.WORKER_HISTORY:
            if self.worker_history is None:
                raise RuntimeError("the worker history route is not configured")
            refusal = await self.worker_history.admit(scope)
            if refusal is not None:
                await refusal(scope, receive, send)
                return
        if matched is not None and matched.name in (
            RouteName.WORKERS,
            RouteName.WORKER_CLAIMS,
            RouteName.USAGE,
        ):
            body = bytearray()
            try:
                async for chunk in Request(scope, receive).stream():
                    if len(chunk) > 4 * 1024 * 1024 - len(body):
                        await error_response(
                            definitive_error(
                                ApiErrorCode.VALIDATION, request_id=exchange.request_id
                            ),
                            method=method,
                        )(scope, receive, send)
                        return
                    body.extend(chunk)
            except ClientDisconnect:
                await error_response(
                    request_rejection(
                        RequestRejection.INCOMPLETE_BODY, request_id=exchange.request_id
                    ),
                    method=method,
                )(scope, receive, send)
                return

            async def received_body() -> Message:
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            await self.application(scope, received_body, send)
        else:
            await self.application(scope, receive, send)


def create_application(
    authenticator: Authenticator,
    diagnostics: ProcessDiagnostics,
    clock: Clock,
    request_id: Callable[[], str],
    deployment: GraphServices | None = None,
    *,
    worker_registration: WorkerRegistrationRoute | None = None,
    worker_claims: WorkerClaimsRoute | None = None,
    worker_history: WorkerHistoryRoute | None = None,
    worker_usage: WorkerUsageRoute | None = None,
    admission_initialization: AdmissionInitialization | None = None,
) -> ApiExchange:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None]:
        if admission_initialization is not None:
            await admission_initialization.initialize(diagnostics)
        yield

    framework = FastAPI(
        lifespan=lifespan,
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
    if deployment is not None:
        framework.add_api_route(
            RouteName.GRAPH_VERSIONS.value,
            deployment_handler(deployment, clock),
            methods=["POST"],
            operation_id="deploy_graph",
            status_code=201,
            response_model=DeployResponse,
            responses={
                200: {
                    "model": DeployResponse,
                    "description": "An equal version exists.",
                }
            },
            openapi_extra={
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": DeployRequest.model_json_schema()
                        }
                    },
                }
            },
        )

        framework.add_api_route(
            RouteName.GRAPH_RUNS.value,
            trigger_handler(deployment, clock),
            methods=["POST"],
            operation_id="trigger_run",
            status_code=201,
            response_model=AcceptedRun,
            responses={
                200: {
                    "model": AcceptedRun | SkippedRun,
                    "description": "A saved outcome or an overlap skip.",
                }
            },
            openapi_extra={
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": TriggerRequest.model_json_schema()
                        }
                    },
                }
            },
        )

    if worker_registration is not None:

        async def register(body: WorkerRegistration) -> RegisteredWorker | Response:
            return await worker_registration(body)

        framework.add_api_route(
            RouteName.WORKERS.value,
            register,
            methods=["POST"],
            operation_id="register_worker",
            response_model=RegisteredWorker,
        )
    if worker_claims is not None:

        async def claim(worker_id: WorkerName, body: WorkerClaimRequest) -> Response:
            return await worker_claims(worker_id, body)

        framework.add_api_route(
            RouteName.WORKER_CLAIMS.value,
            claim,
            methods=["POST"],
            operation_id="claim_worker",
            response_model=WorkerClaimResponse,
        )
    if worker_history is not None:

        async def history(att: Identifier, request: Request) -> Response:
            return await worker_history(att, request)

        properties = WorkerHistoryQuery.model_json_schema()["properties"]
        framework.add_api_route(
            RouteName.WORKER_HISTORY.value,
            history,
            methods=["GET"],
            operation_id="grant_history",
            response_model=GrantHistoryPage,
            openapi_extra={
                "parameters": [
                    {
                        "name": name,
                        "in": "query",
                        "required": field.is_required(),
                        "schema": properties[name],
                    }
                    for name, field in WorkerHistoryQuery.model_fields.items()
                ]
            },
        )
    if worker_usage is not None:

        async def usage(
            att: Identifier, body: UsageReport
        ) -> AcceptedUsageReport | Response:
            return await worker_usage(att, body)

        framework.add_api_route(
            RouteName.USAGE.value,
            usage,
            methods=["POST"],
            operation_id="report_usage",
            response_model=AcceptedUsageReport,
        )
    framework.add_exception_handler(HTTPException, framework_error)
    framework.add_exception_handler(RequestValidationError, validation_error)
    framework.add_exception_handler(Exception, unexpected_error)
    framework.add_middleware(
        ApiDispatch,
        catalog=ActiveCatalog(framework),
        authenticator=authenticator,
        clock=clock,
        worker_history=worker_history,
    )
    return ApiExchange(framework, diagnostics, request_id)
