import asyncio
import json
from contextlib import AsyncExitStack

import pytest
import structlog.contextvars
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from opentelemetry import baggage, trace
from opentelemetry import context as telemetry
from starlette.responses import JSONResponse, Response
from starlette.types import Message

from kora.api.exchange import COMPONENT, ApiExchange, current_exchange
from kora.api.responses import framework_error, unexpected_error, validation_error
from kora.conformance.capture import Capture, captured
from kora.db.connection import SQLITE_READERS, Database, Dialect
from kora.models.errors import ApiErrorEnvelope
from kora.observability import (
    Level,
    LoggingSettings,
    RenderFormat,
    bound_scope,
    current_context,
)
from tests.api.support import (
    POISON,
    HttpCall,
    RequestIds,
    api_capture,
    application,
    client_for,
)
from tests.db.closing import ConnectionLedger
from tests.plugins.authentication.support import AuthorityClock, seed_case
from tests.support.event_loop import turn

__all__ = ["api_capture"]


def owned_framework() -> FastAPI:
    framework = FastAPI(
        debug=False,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        telemetry={
            "tracing": False,
            "metrics": False,
            "logs": False,
            "operation_spans": False,
            "auto_configure": False,
        },
    )
    framework.add_exception_handler(HTTPException, framework_error)
    framework.add_exception_handler(RequestValidationError, validation_error)
    framework.add_exception_handler(Exception, unexpected_error)
    return framework


@pytest.mark.asyncio
async def test_api_request_id_matches_response_error_and_logs(
    database: Database, api_capture: Capture
) -> None:
    app = application(database, api_capture)
    paths = ["/healthz", "/missing", "/api/v1/openapi.json"]
    marker = object()
    previous = structlog.contextvars.get_contextvars()
    structlog.contextvars.bind_contextvars(private_claim=marker)
    try:
        with bound_scope(
            namespace_id="old_namespace", run_id="old_run", request_id="old_request"
        ):
            async with client_for(app) as client:
                responses = await asyncio.gather(
                    *(
                        client.get(path, headers={"X-Request-ID": POISON})
                        for path in paths
                    )
                )
            assert (
                structlog.contextvars.get_contextvars()["namespace_id"]
                == "old_namespace"
            )
            assert structlog.contextvars.get_contextvars()["private_claim"] is marker
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**previous)
    records = [
        record
        for record in api_capture.finish()
        if record["event"] == "api.request.completed"
    ]

    assert len(records) == 3
    assert {record["request_id"] for record in records} == {
        response.headers["x-request-id"] for response in responses
    }
    assert all(
        "namespace_id" not in record and "run_id" not in record for record in records
    )
    assert POISON not in json.dumps(records)
    for response in responses[1:]:
        assert (
            ApiErrorEnvelope.model_validate_json(response.content).error.request_id
            == response.headers["x-request-id"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/healthz", "/api/v1/openapi.json", "/missing"])
async def test_api_ingress_does_not_consume_or_log_body(
    database: Database, api_capture: Capture, path: str
) -> None:
    app = application(database, api_capture)
    call = HttpCall(
        path,
        headers=[(b"cookie", POISON.encode()), (b"x-request-id", POISON.encode())],
        query=POISON.encode(),
    )

    await call.run(app)
    records = api_capture.finish()

    assert call.receives == 0
    assert POISON.encode() not in call.body
    assert POISON not in json.dumps(records)


@pytest.mark.asyncio
@pytest.mark.parametrize("render", [RenderFormat.JSON, RenderFormat.TEXT])
@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
async def test_api_diagnostics_exclude_independent_ingress_values(
    database: Database, render: RenderFormat, level: Level
) -> None:
    private_path = "/private-path-marker"
    private_method = "private-method-marker"
    markers = [
        private_path,
        private_method,
        "private-query-marker",
        "private-authorization-marker",
        "private-cookie-marker",
        "private-caller-identity-marker",
    ]
    headers = [
        (b"Authorization", markers[3].encode()),
        (b"Cookie", markers[4].encode()),
        (b"X-Request-ID", markers[5].encode()),
    ]
    clock = AuthorityClock()
    calls = [
        HttpCall(private_path, method, headers, query=markers[2].encode())
        for method in ["GET", private_method]
    ]
    with captured(
        COMPONENT,
        LoggingSettings(level=level, render_format=render),
        "api-ingress",
        clock.now,
    ) as capture:
        app = application(database, capture)

        for call in calls:
            await call.run(app)
        capture.finish()

        assert [call.status for call in calls] == [404, 501]
        assert all(call.receives == 0 for call in calls)
        assert capture.reader.data.count(b"api.request.completed") == (
            2 if level == Level.DEBUG else 0
        )
        for marker in markers:
            assert marker.encode() not in capture.reader.data
            assert all(marker.encode() not in call.body for call in calls)
    assert not capture.diagnostics.writer_is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unexpected", "framework", "validation"])
@pytest.mark.parametrize("render", [RenderFormat.JSON, RenderFormat.TEXT])
async def test_api_framework_errors_are_content_free(
    kind: str, render: RenderFormat
) -> None:
    framework = owned_framework()

    async def unexpected() -> Response:
        raise RuntimeError(POISON)

    async def rejected() -> Response:
        raise HTTPException(
            418, detail={POISON: POISON}, headers={"private-header": POISON}
        )

    async def validated(value: int) -> JSONResponse:
        return JSONResponse({"value": value})

    framework.add_api_route("/unexpected", unexpected)
    framework.add_api_route("/framework", rejected)
    framework.add_api_route("/validation", validated)
    clock = AuthorityClock()
    call = HttpCall("/" + kind, query=("value=" + POISON).encode())
    with captured(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG, render_format=render),
        "api-errors",
        clock.now,
    ) as capture:
        app = ApiExchange(framework, capture.diagnostics, RequestIds())

        if kind == "unexpected":
            with pytest.raises(RuntimeError, match=POISON):
                await call.run(app)
        else:
            await call.run(app)
        capture.finish()

        assert call.status == (500 if kind == "unexpected" else 422)
        assert POISON.encode() not in call.body
        assert POISON.encode() not in capture.reader.data
        assert b"private-header" not in dict(call.messages[0]["headers"])
        assert (
            ApiErrorEnvelope.model_validate_json(call.body).error.request_id
            == "req_api_1"
        )
    assert not capture.diagnostics.writer_is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["success", "failure", "cancel", "send"])
async def test_api_context_is_restored_after_every_exit(
    api_capture: Capture, stage: str
) -> None:
    framework = owned_framework()
    entered = asyncio.Event()
    release = asyncio.Event()
    observed: list[object] = []

    async def endpoint() -> Response:
        observed.append(telemetry.get_current())
        observed.append(current_context())
        if stage == "failure":
            raise RuntimeError(POISON)
        if stage == "cancel":
            entered.set()
            await release.wait()
        response = Response()
        response.raw_headers.extend(
            [(b"X-Request-ID", POISON.encode()), (b"x-request-id", POISON.encode())]
        )
        return response

    framework.add_api_route("/context", endpoint)
    app = ApiExchange(framework, api_capture.diagnostics, RequestIds())
    call = HttpCall("/context")
    original = baggage.set_baggage("private-api-baggage", POISON, telemetry.Context())
    span = trace.NonRecordingSpan(
        trace.SpanContext(trace_id=1, span_id=1, is_remote=True)
    )
    original = trace.set_span_in_context(span, original)
    original = telemetry.set_value("private-api-context", POISON, original)
    token = telemetry.attach(original)
    restored: list[bool] = []

    async def operation() -> None:
        try:
            with bound_scope(namespace_id="outside_namespace"):
                try:
                    if stage == "send":

                        async def refused_send(message: Message) -> None:
                            raise RuntimeError(POISON)

                        await app(call.scope(), call.receive, refused_send)
                    else:
                        await call.run(app)
                finally:
                    restored.append(
                        current_context() == {"namespace_id": "outside_namespace"}
                    )
                    restored.append(telemetry.get_current() is original)
        finally:
            assert telemetry.get_current() is original

    try:
        if stage == "cancel":
            task = asyncio.create_task(operation())
            try:
                await asyncio.wait_for(entered.wait(), 10)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        elif stage in {"failure", "send"}:
            with pytest.raises(RuntimeError, match=POISON):
                await operation()
        else:
            await operation()
    finally:
        telemetry.detach(token)

    assert observed[0] == telemetry.Context()
    assert observed[1] == {"request_id": "req_api_1"}
    assert restored == [True, True]
    with pytest.raises(RuntimeError, match="An API exchange is required"):
        current_exchange()
    if stage == "success":
        assert [
            pair
            for pair in call.messages[0]["headers"]
            if pair[0].lower() == b"x-request-id"
        ] == [(b"x-request-id", b"req_api_1")]


@pytest.mark.asyncio
async def test_api_cancellation_releases_borrowed_resources(
    database: Database, api_capture: Capture
) -> None:
    case = await seed_case(database, "api-cancel")
    app = application(database, api_capture)
    ledger = ConnectionLedger(database)
    capacity = (
        SQLITE_READERS
        if database.dialect is Dialect.SQLITE
        else database.settings.pool_size + database.settings.max_overflow
    )
    held = AsyncExitStack()
    for _ in range(capacity):
        await held.enter_async_context(database.read_transaction())
    started = asyncio.Event()
    call = HttpCall(
        "/api/v1/openapi.json",
        headers=[(b"authorization", str(case.request.authorization).encode())],
    )

    async def request() -> None:
        started.set()
        await call.run(app)

    task = asyncio.create_task(request())
    try:
        await started.wait()
        await turn()
        assert not task.done()
        assert ledger.outstanding == capacity
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await held.aclose()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert ledger.outstanding == 0
    async with client_for(app) as client:
        response = await client.get(
            "/api/v1/openapi.json",
            headers={"Authorization": str(case.request.authorization)},
        )
    assert response.status_code == 200
    assert ledger.outstanding == 0
    assert api_capture.diagnostics.writer_is_alive()
