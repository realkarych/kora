import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from opentelemetry import context
from opentelemetry.trace import ProxyTracerProvider
from pydantic import StrictBool, StrictInt
from starlette.responses import Response
from starlette.types import Message

from kora.api.application import create_application
from kora.api.exchange import COMPONENT
from kora.conformance.capture import captured
from kora.db.connection import Database
from kora.observability import Level, LoggingSettings
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from kora.protocols.boundary import BoundaryModel
from tests.api.support import POISON, HttpCall, RequestIds, lifespan
from tests.plugins.authentication.support import AuthorityClock
from tests.support.databases import sqlite_settings


class TelemetryReport(BoundaryModel):
    status: StrictInt
    warnings: StrictInt
    failed_requests: StrictInt
    restored: StrictBool
    writer_alive: StrictBool
    sdk_loaded: StrictBool


async def exercise(mode: str) -> TelemetryReport:
    clock = AuthorityClock()
    settings = LoggingSettings(
        level=Level.DEBUG,
        third_party={"fastapi": Level.DEBUG, "opentelemetry": Level.DEBUG},
    )
    with (
        TemporaryDirectory(prefix="kora-api-telemetry-") as directory,
        captured(COMPONENT, settings, "api-telemetry", clock.now) as capture,
    ):
        database = Database(sqlite_settings(Path(directory) / "unused.sqlite"))
        if mode == "owned":
            app = create_application(
                StaticTokenAuthenticator({}, database),
                capture.diagnostics,
                clock,
                RequestIds(),
            )
        else:
            provider = ProxyTracerProvider() if mode == "control-auto" else None
            app = FastAPI(
                telemetry={
                    "tracing": True,
                    "tracer_provider": provider,
                    "metrics": False,
                    "logs": False,
                    "operation_spans": False,
                    "auto_configure": mode == "control-auto",
                }
            )

            async def health() -> Response:
                return Response()

            app.add_api_route("/healthz", health)
        outer = context.set_value("private-outer", POISON, context.Context())
        token = context.attach(outer)
        call = HttpCall()
        restored = False
        try:
            async with lifespan(app):
                await call.run(app)
                if mode == "owned":

                    async def failed_send(message: Message) -> None:
                        raise RuntimeError(POISON)

                    failure: RuntimeError | None = None
                    try:
                        await app(HttpCall().scope(), call.receive, failed_send)
                    except RuntimeError as error:
                        failure = error
                    assert failure is not None
                    assert str(failure) == POISON
            restored = context.get_current() is outer
        finally:
            context.detach(token)
            await database.close()
        records = capture.finish()
        assert POISON.encode() not in capture.reader.data
        return TelemetryReport(
            status=call.status,
            warnings=sum(
                record["component"] == "thirdparty"
                and record["level"] in {"WARNING", "ERROR", "CRITICAL"}
                for record in records
            ),
            failed_requests=sum(
                record["event"] == "api.request.failed" for record in records
            ),
            restored=restored,
            writer_alive=capture.diagnostics.writer_is_alive(),
            sdk_loaded=any(
                name.startswith(("opentelemetry.sdk", "opentelemetry.exporter"))
                for name in sys.modules
            ),
        )


if __name__ == "__main__":
    sys.stdout.write(asyncio.run(exercise(sys.argv[1])).model_dump_json() + "\n")
