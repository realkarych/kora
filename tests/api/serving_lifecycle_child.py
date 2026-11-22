import asyncio
import errno
import json
import os
import socket
import sys
import threading
from asyncio.trsock import TransportSocket
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Protocol, cast

import aiosqlite
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from kora.__main__ import serving_deployment_preparer
from kora.api.exchange import COMPONENT
from kora.api.graph_deployment_worker import THREAD_NAME, DeploymentPreparer
from kora.api.serving import serve_sqlite
from kora.cel_eval.evaluate import PatternCompilation
from kora.cel_eval.thread import PROCESS, Evaluator
from kora.db.connection import Database, DatabaseClosedError, read_database_settings
from kora.observability import configure_process, read_logging_settings
from kora.protocols.time import SystemClock

PRIVATE_LIFESPAN_REASON: str = "private-lifespan-startup-reason"
SLOW_RESPONSE_BYTES: int = 4 * 1024 * 1024


class SqliteTraceControl(Protocol):
    async def set_trace_callback(
        self, handler: Callable[[str], None] | None
    ) -> None: ...


def owns_sqlite_descriptor(path: Path) -> bool:
    expected = path.stat()
    inventory = "/dev/fd" if sys.platform == "darwin" else "/proc/self/fd"
    for name in os.listdir(inventory):
        try:
            held = os.fstat(int(name))
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
            continue
        if (held.st_dev, held.st_ino) == (expected.st_dev, expected.st_ino):
            return True
    return False


async def hold_native_query(
    connection: AsyncConnection, release: int, path: Path
) -> None:
    def observe(statement: str) -> None:
        if statement == "SELECT 42":
            assert owns_sqlite_descriptor(path)
            os.write(1, b"native_sqlite_held\n")
            if os.read(release, 1) == b"r":
                os.write(1, b"native_sqlite_returned\n")

    driver = (await connection.get_raw_connection()).driver_connection
    assert isinstance(driver, aiosqlite.Connection)
    await cast(SqliteTraceControl, driver).set_trace_callback(observe)
    with await connection.exec_driver_sql("SELECT 42") as result:
        result.scalar_one()
    os.write(1, b"native_sqlite_result_closed\n")


async def lifecycle(receive: Receive, send: Send) -> None:
    await receive()
    await send({"type": "lifespan.startup.complete"})
    await receive()
    os.write(1, b"lifespan_shutdown\n")
    await send({"type": "lifespan.shutdown.complete"})


def held_request_app(database: Database, release: int | None = None) -> ASGIApp:
    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifecycle(receive, send)
            return
        try:
            async with database.read_transaction() as connection:
                if release is not None:
                    await hold_native_query(
                        connection, release, database.settings.sqlite_path
                    )
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


async def hold_preparation(release: int, evaluator: Evaluator) -> None:
    assert threading.current_thread().name == THREAD_NAME
    assert evaluator.snapshot().handles == 1
    os.write(1, b"preparation_held\n")
    assert await asyncio.to_thread(os.read, release, 1) == b"r"
    assert await evaluator.validate_pattern("^ready$") is PatternCompilation.ACCEPTED
    os.write(1, b"retained_cel_settled\n")


def held_preparation_app(
    database: Database, preparer: DeploymentPreparer, release: int
) -> ASGIApp:
    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifecycle(receive, send)
            return
        async with preparer.reserve() as reservation:
            assert reservation is not None
            preparation = asyncio.create_task(
                reservation.run(partial(hold_preparation, release))
            )
            try:
                while (await receive())["type"] != "http.disconnect":
                    continue
                async with database.transaction() as connection:
                    with await connection.exec_driver_sql("SELECT 42") as result:
                        assert result.scalar_one() == 42
            finally:
                assert preparation.cancel()
                (cancelled,) = await asyncio.gather(preparation, return_exceptions=True)
                assert isinstance(cancelled, asyncio.CancelledError)
        assert preparer.busy
        os.write(1, b"request_sqlite_settled\n")

    return application


def buffered_response(database: Database) -> ASGIApp:
    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifecycle(receive, send)
            return
        installed_transports: object = vars(asyncio.get_running_loop())["_transports"]
        assert isinstance(installed_transports, Mapping)
        inventory = cast("Mapping[int, object]", installed_transports)
        original = next(
            active
            for active in inventory.values()
            if isinstance(active, asyncio.Transport)
            and active.get_extra_info("peername") == scope["client"]
        )
        original_socket: object = original.get_extra_info("socket")
        assert isinstance(original_socket, TransportSocket)
        with socket.socket(fileno=os.dup(original_socket.fileno())) as control:
            control.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
            assert control.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) <= 65536
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", str(SLOW_RESPONSE_BYTES).encode())],
            }
        )
        await send({"type": "http.response.body", "body": b"x" * SLOW_RESPONSE_BYTES})
        assert original.get_write_buffer_size() > 1024 * 1024
        os.write(1, b"response_buffered\n")

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
    preparer = None
    try:
        match sys.argv[1:]:
            case ["refuse_lifespan"]:
                make_application = refused_lifespan
            case ["hold_native", release]:
                make_application = partial(held_request_app, release=int(release))
            case ["hold_preparation", release]:
                preparer = serving_deployment_preparer(SystemClock())
                make_application = partial(
                    held_preparation_app, preparer=preparer, release=int(release)
                )
            case ["buffered_response"]:
                make_application = buffered_response
            case _:
                make_application = held_request_app
        result = serve_sqlite(
            settings,
            0,
            diagnostics.logger(COMPONENT),
            make_application,
            preparer=preparer,
        )
        if sys.argv[1:2] == ["hold_native"]:
            assert not owns_sqlite_descriptor(settings.sqlite_path)
            os.write(1, b"native_sqlite_file_closed\n")
        if preparer is not None:
            owner = PROCESS.thread
            assert owner is not None
            snapshot = owner.snapshot()
            assert snapshot.handles == snapshot.outstanding == snapshot.bytes == 0
            assert all(thread.name != THREAD_NAME for thread in threading.enumerate())
            assert not preparer.busy
            assert not owns_sqlite_descriptor(settings.sqlite_path)
            os.write(1, b"domain_owners_settled\n")
        return result
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
