import asyncio

import pytest
from fastapi import FastAPI
from fastapi.telemetry import get_telemetry_data
from opentelemetry import context
from opentelemetry.trace import ProxyTracerProvider
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from kora.api.exchange import HTTP_SCOPE_FIELDS, ApiExchange
from kora.conformance.capture import Capture
from tests.api.support import POISON, HttpCall, RequestIds, api_capture
from tests.api.telemetry_probe import TelemetryReport
from tests.api.test_exchange import owned_framework
from tests.support.observability import run_python

__all__ = ["api_capture"]

TRAPS: dict[str, str] = {
    "FASTAPI_OTEL_AUTO_CONFIGURE": "true",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_PYTHON_TRACER_PROVIDER": "missing-api-tracer",
    "OTEL_PYTHON_METER_PROVIDER": "missing-api-meter",
    "OTEL_PYTHON_LOGGER_PROVIDER": "missing-api-logger",
}


def test_api_native_telemetry_is_disabled() -> None:
    process = run_python(["-m", "tests.api.telemetry_probe", "owned"], TRAPS)

    assert process.code == 0, process.stderr
    report = TelemetryReport.model_validate_json(process.stdout)

    assert process.stderr == b""
    assert report.status == 200
    assert report.warnings == 0
    assert report.failed_requests == 1
    assert report.restored
    assert not report.writer_alive
    assert not report.sdk_loaded


def test_api_telemetry_environment_cannot_enable_export() -> None:
    process = run_python(["-m", "tests.api.telemetry_probe", "control-auto"], TRAPS)

    assert process.code == 0, process.stderr
    report = TelemetryReport.model_validate_json(process.stdout)

    assert report.status == 200
    assert report.warnings > 0
    assert not report.writer_alive
    assert not report.sdk_loaded


def test_native_provider_lookup_positive_control_is_sensitive() -> None:
    process = run_python(["-m", "tests.api.telemetry_probe", "control-provider"], TRAPS)

    assert process.code != 0
    assert b"_load_provider" in process.stderr
    assert b"get_tracer_provider" in process.stderr


class ProjectionProbe:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.observed: dict[str, object] = {}
        self.context_empty = False
        self.telemetry_empty = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            self.observed = dict(scope)
            self.context_empty = context.get_current() == context.Context()
            self.telemetry_empty = get_telemetry_data() is None
        await self.app(scope, receive, send)


class NativeBoundary:
    def __init__(self, app: ASGIApp, projection: ProjectionProbe) -> None:
        self.app = app
        self.projection = projection
        self.inherited = False
        self.restored = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        native = get_telemetry_data()
        self.inherited = native is not None
        original = dict(scope)
        try:
            await self.app(scope, receive, send)
        finally:
            self.restored = get_telemetry_data() is native
            assert dict(scope) == original
            assert set(self.projection.observed) == set(original).intersection(
                HTTP_SCOPE_FIELDS
            )
            assert all(
                self.projection.observed[key] is value
                for key, value in original.items()
                if key in HTTP_SCOPE_FIELDS
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["success", "failure", "cancel"])
async def test_api_telemetry_context_is_isolated_and_restored(
    api_capture: Capture, stage: str
) -> None:
    inner = owned_framework()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def endpoint() -> Response:
        assert get_telemetry_data() is None
        assert context.get_current() == context.Context()
        if stage == "failure":
            raise RuntimeError(POISON)
        if stage == "cancel":
            entered.set()
            await release.wait()
        return Response()

    inner.add_api_route("/entry", endpoint)
    projection = ProjectionProbe(inner)
    boundary = NativeBoundary(
        ApiExchange(projection, api_capture.diagnostics, RequestIds()), projection
    )
    outer = FastAPI(
        telemetry={
            "tracing": True,
            "tracer_provider": ProxyTracerProvider(),
            "metrics": False,
            "logs": False,
            "operation_spans": False,
            "auto_configure": False,
        }
    )
    outer.mount("/inner", boundary)
    call = HttpCall("/inner/entry")
    scope = call.scope()
    scope.update(
        {
            "state": {"opaque": object()},
            "extensions": {"opaque": object()},
            "untrusted.private": POISON,
        }
    )
    token = context.attach(
        context.set_value("private-baggage", POISON, context.Context())
    )
    try:
        if stage == "cancel":
            task = asyncio.create_task(outer(scope, call.receive, call.send))
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
        elif stage == "failure":
            with pytest.raises(RuntimeError, match=POISON):
                await outer(scope, call.receive, call.send)
        else:
            await outer(scope, call.receive, call.send)
    finally:
        context.detach(token)

    assert boundary.inherited
    assert boundary.restored
    assert projection.context_empty
    assert projection.telemetry_empty
    assert "untrusted.private" not in projection.observed
