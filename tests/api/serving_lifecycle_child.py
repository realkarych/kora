import asyncio
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Protocol, cast

import aiosqlite
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from kora.api.exchange import COMPONENT
from kora.api.serving import serve_sqlite
from kora.db.connection import Database, DatabaseClosedError, read_database_settings
from kora.observability import configure_process, read_logging_settings

PRIVATE_LIFESPAN_REASON: str = "private-lifespan-startup-reason"


class SqliteTraceControl(Protocol):
    async def set_trace_callback(
        self, handler: Callable[[str], None] | None
    ) -> None: ...


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
    os.write(1, b"native_sqlite_result_closed\n")


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
                    with await connection.exec_driver_sql("SELECT 1") as result:
                        result.scalar_one()
                    os.write(1, b"request_holds_sqlite\n")
                while (await receive())["type"] != "http.disconnect":
                    continue
        except DatabaseClosedError:
            if release is None:
                raise
        os.write(1, b"request_settled\n")

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


async def serving_event(
    stderr: asyncio.StreamReader, event_name: str
) -> dict[object, object]:
    while line := await stderr.readline():
        event: object = json.loads(line)
        if isinstance(event, dict):
            fields = cast("dict[object, object]", event)
            if fields.get("event") == event_name:
                return fields
    raise AssertionError(f"the child exited before {event_name}")


if __name__ == "__main__":
    sys.exit(launch())
