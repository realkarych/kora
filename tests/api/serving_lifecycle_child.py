import asyncio
import os
import sys
from datetime import UTC, datetime
from functools import partial
from typing import cast

import aiosqlite
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from kora.api.exchange import COMPONENT
from kora.api.serving import serve_sqlite
from kora.db.connection import Database, read_database_settings
from kora.observability import configure_process, read_logging_settings
from tests.db.native_read_support import SqliteTraceControl

PRIVATE_LIFESPAN_REASON: str = "private-lifespan-startup-reason"


async def hold_native_query(connection: AsyncConnection, release: int) -> None:
    def observe(statement: str) -> None:
        if statement == "SELECT 42":
            os.write(1, b"native_sqlite_held\n")
            if os.read(release, 1) == b"r":
                os.write(1, b"native_sqlite_returned\n")

    driver = (await connection.get_raw_connection()).driver_connection
    assert isinstance(driver, aiosqlite.Connection)
    await cast(SqliteTraceControl, driver).set_trace_callback(observe)
    with await connection.exec_driver_sql("SELECT 42") as result:
        result.scalar_one()


def held_request_app(database: Database, release: int | None = None) -> ASGIApp:
    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            os.write(1, b"lifespan_shutdown\n")
            await send({"type": "lifespan.shutdown.complete"})
            return
        try:
            async with database.read_transaction() as connection:
                if release is not None:
                    await hold_native_query(connection, release)
                else:
                    await connection.exec_driver_sql("SELECT 1")
                    os.write(1, b"request_holds_sqlite\n")
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
        match sys.argv[1:]:
            case ["refuse_lifespan"]:
                make_application = refused_lifespan
            case ["hold_native", release]:
                make_application = partial(held_request_app, release=int(release))
            case _:
                make_application = held_request_app
        return serve_sqlite(
            settings, 0, diagnostics.logger(COMPONENT), make_application
        )
    finally:
        diagnostics.shutdown()


if __name__ == "__main__":
    sys.exit(launch())
