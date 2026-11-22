import asyncio
import os
import sys
from datetime import UTC, datetime

from starlette.types import ASGIApp, Receive, Scope, Send

from kora.api.exchange import COMPONENT
from kora.api.serving import serve_sqlite
from kora.db.connection import Database, read_database_settings
from kora.observability import configure_process, read_logging_settings

PRIVATE_LIFESPAN_REASON: str = "private-lifespan-startup-reason"


def held_request_app(database: Database) -> ASGIApp:
    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            os.write(1, b"lifespan_shutdown\n")
            await send({"type": "lifespan.shutdown.complete"})
            return
        async with database.read_transaction() as connection:
            await connection.exec_driver_sql("SELECT 1")
            os.write(1, b"request_holds_sqlite\n")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                os.write(1, b"request_cancelled\n")
                raise

    return application


def refused_lifespan(database: Database) -> ASGIApp:
    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError(PRIVATE_LIFESPAN_REASON)

    return application


def now() -> datetime:
    return datetime.now(UTC)


def launch() -> int:
    settings = read_database_settings(os.environ)
    diagnostics = configure_process(
        COMPONENT, read_logging_settings(os.environ, (COMPONENT,)), "api-lifecycle", now
    )
    try:
        make_application = (
            refused_lifespan
            if sys.argv[1:] == ["refuse_lifespan"]
            else held_request_app
        )
        return serve_sqlite(
            settings, 0, diagnostics.logger(COMPONENT), make_application
        )
    finally:
        diagnostics.shutdown()


if __name__ == "__main__":
    sys.exit(launch())
