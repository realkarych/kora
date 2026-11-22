import asyncio
import errno
import json
import os
import signal
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
from sqlalchemy import event
from sqlalchemy.engine import AdaptedConnection, Connection
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kora.__main__ import new_instance_id, serving_deployment_preparer
from kora.api.application import create_application
from kora.api.exchange import COMPONENT
from kora.api.graph_deployment_worker import THREAD_NAME, DeploymentPreparer
from kora.api.graph_services import deployment_selection
from kora.api.installed_scheduler import installed_root_admission
from kora.api.installed_worker import installed_worker_profile
from kora.api.serving import serve_sqlite
from kora.api.worker_claims_route import WorkerClaimsRoute
from kora.api.worker_registration_route import WorkerRegistrationRoute
from kora.cel_eval.evaluate import PatternCompilation
from kora.cel_eval.thread import PROCESS, Evaluator
from kora.db.connection import (
    Database,
    DatabaseClosedError,
    Lifecycle,
    read_database_settings,
)
from kora.db.identifiers import IdentifierGenerator
from kora.engine.admission_initialization import AdmissionInitialization
from kora.engine.worker_claim_poll import WorkerClaimPoll
from kora.observability import (
    ProcessDiagnostics,
    configure_process,
    read_logging_settings,
)
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from kora.plugins.runtime.local.configuration import LocalRuntimeConfiguration
from kora.protocols.time import SystemClock
from kora.scheduler.loop import Scheduler, first_claim_scheduler

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


def await_native_release(path: Path, release: int, *, native_sigterm: bool) -> None:
    assert owns_sqlite_descriptor(path)
    os.write(1, b"native_sqlite_held\n")
    if native_sigterm:
        assert os.read(release, 1) == b"s"
        signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
        os.write(1, b"native_sqlite_sigterm_sent\n")
    if os.read(release, 1) == b"r":
        os.write(1, b"native_sqlite_returned\n")


async def hold_native_query(
    connection: AsyncConnection,
    release: int,
    path: Path,
    *,
    native_sigterm: bool = False,
) -> None:
    def observe(statement: str) -> None:
        if statement == "SELECT 42":
            await_native_release(path, release, native_sigterm=native_sigterm)

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


def held_request_app(
    database: Database, release: int | None = None, *, scheduler_retained: bool = False
) -> ASGIApp:
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
                    if scheduler_retained:
                        assert database.units_in_flight() == 2
                    os.write(1, b"request_holds_sqlite\n")
                while (await receive())["type"] != "http.disconnect":
                    continue
        except DatabaseClosedError:
            if release is None:
                raise
        if scheduler_retained:
            assert database.units_in_flight() == 1
            assert database.lifecycle is Lifecycle.OPEN
            assert owns_sqlite_descriptor(database.settings.sqlite_path)
        os.write(1, b"request_settled\n")

    return application


async def hold_preparation(
    database: Database, release: int, evaluator: Evaluator
) -> None:
    assert threading.current_thread().name == THREAD_NAME
    assert evaluator.snapshot().handles == 1
    os.write(1, b"preparation_held\n")
    released = await asyncio.to_thread(os.read, release, 1)
    assert released == b"r"
    assert database.lifecycle is Lifecycle.OPEN
    assert owns_sqlite_descriptor(database.settings.sqlite_path)
    outcome = await evaluator.validate_pattern("^ready$")
    assert outcome is PatternCompilation.ACCEPTED
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
                reservation.run(partial(hold_preparation, database, release))
            )
            try:
                while (await receive())["type"] != "http.disconnect":
                    continue
                async with database.transaction() as connection:
                    with await connection.exec_driver_sql("SELECT 42") as result:
                        assert result.scalar_one() == 42
            finally:
                cancellation_requested = preparation.cancel()
                assert cancellation_requested
                (cancelled,) = await asyncio.gather(preparation, return_exceptions=True)
                assert isinstance(cancelled, asyncio.CancelledError)
        assert preparer.busy
        os.write(1, b"request_sqlite_settled\n")

    return application


def limit_socket_send_buffer(scope: Scope) -> asyncio.Transport:
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
    return original


def buffered_response(database: Database) -> ASGIApp:
    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifecycle(receive, send)
            return
        original = limit_socket_send_buffer(scope)
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


class HeldWorkerRecovery(WorkerClaimPoll):
    release: int
    native_sigterm: bool

    async def _recover_run_expiry(self, run_id: str) -> None:
        async with self.database.read_transaction() as reader:
            await hold_native_query(
                reader,
                self.release,
                self.database.settings.sqlite_path,
                native_sigterm=self.native_sigterm,
            )
        await super()._recover_run_expiry(run_id)


def worker_lifecycle_app(
    database: Database,
    diagnostics: ProcessDiagnostics,
    release: int,
    recovery: bool,
    native_sigterm: bool,
    scheduler_http_hold: bool = False,
) -> ASGIApp:
    profile = installed_worker_profile("2000")
    assert profile is not None
    clock = SystemClock()
    runtimes = {"local": LocalRuntimeConfiguration(state_root=None).capabilities()}
    arguments = database, runtimes, clock, profile.limits, profile.uncertainty
    poller: WorkerClaimPoll
    if recovery:
        held_recovery = HeldWorkerRecovery(*arguments)
        held_recovery.release = release
        held_recovery.native_sigterm = native_sigterm
        poller = held_recovery
    else:
        poller = WorkerClaimPoll(*arguments)
    application = create_application(
        StaticTokenAuthenticator({}, database),
        diagnostics,
        clock,
        new_instance_id,
        worker_registration=WorkerRegistrationRoute(
            database, runtimes, clock, diagnostics
        ),
        worker_claims=WorkerClaimsRoute(poller),
        admission_initialization=AdmissionInitialization(
            database, clock, IdentifierGenerator(clock.now).new
        ),
        single_claim=True,
    )
    held = False
    held_http = (
        held_request_app(database, scheduler_retained=True)
        if scheduler_http_hold
        else None
    )

    async def exercise(scope: Scope, receive: Receive, send: Send) -> None:
        if held_http is not None and scope["type"] == "http":
            await held_http(scope, receive, send)
            return
        claim = (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"].endswith("/claims")
        )
        successful = False
        original: asyncio.Transport | None = None

        async def observe(message: Message) -> None:
            nonlocal held, original, successful
            match message["type"]:
                case "lifespan.shutdown.complete":
                    assert owns_sqlite_descriptor(database.settings.sqlite_path)
                    os.write(1, b"lifespan_shutdown\n")
                case "http.response.start" if claim:
                    successful = message["status"] == 200
                case "http.response.body" if (
                    claim and successful and not held and not recovery
                ):
                    held = True
                    original = limit_socket_send_buffer(scope)
                    async with database.read_transaction() as reader:
                        await hold_native_query(
                            reader, release, database.settings.sqlite_path
                        )
                case _:
                    pass
            await send(message)

        async def observe_receive() -> Message:
            message = await receive()
            if scheduler_http_hold and message["type"] == "lifespan.shutdown":
                os.write(1, b"lifespan_shutdown_received\n")
            return message

        await application(scope, observe_receive, observe)
        if original is not None:
            assert original.get_write_buffer_size() > 1024 * 1024
            assert owns_sqlite_descriptor(database.settings.sqlite_path)
            os.write(1, b"claim_handed_to_socket\n")

    return exercise


def scheduler_lifecycle_owner(
    database: Database,
    release: int,
    native_sigterm: bool,
    forced_owners: list[Database] | None = None,
) -> Scheduler:
    if forced_owners is not None:
        forced_owners.append(database)
    profile = installed_worker_profile("2000")
    assert profile is not None
    clock = SystemClock()
    capabilities = LocalRuntimeConfiguration(state_root=None).capabilities()
    selected = deployment_selection("claude", capabilities)
    observed = False

    def pending_query(statement: str) -> bool:
        return " ".join(statement.lower().split()).startswith(
            "select run.id from run where run.status ="
        )

    def observe(statement: str) -> None:
        nonlocal observed
        if not observed and pending_query(statement) and "'pending'" in statement:
            observed = True
            await_native_release(
                database.settings.sqlite_path, release, native_sigterm=native_sigterm
            )

    async def trace(driver: object) -> None:
        assert isinstance(driver, aiosqlite.Connection)
        await cast(SqliteTraceControl, driver).set_trace_callback(observe)

    def attach(
        connection: Connection, cursor: object, statement: str, *details: object
    ) -> None:
        if not observed and pending_query(statement):
            adapter: object = connection.connection.dbapi_connection
            assert isinstance(adapter, AdaptedConnection)
            adapter.run_async(trace)

    event.listen(database.reader_engine.sync_engine, "before_cursor_execute", attach)
    admission = installed_root_admission(
        database,
        profile.limits,
        clock.now,
        IdentifierGenerator(clock.now),
        selected,
        capabilities,
    )
    return first_claim_scheduler(admission, read_seconds=profile.limits.read_seconds)


def now() -> datetime:
    return datetime.now(UTC)


def launch() -> int:
    settings = read_database_settings(os.environ)
    diagnostics = configure_process(
        COMPONENT, read_logging_settings(os.environ, (COMPONENT,)), "api-lifecycle", now
    )
    preparer = None
    port = 0
    worker_startup = False
    make_scheduler: Callable[[Database], Scheduler] | None = None
    forced_owners: list[Database] = []
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
            case [
                (
                    "hold_worker_recovery"
                    | "hold_worker_recovery_native_term"
                    | "hold_claim_response"
                ) as mode,
                requested_port,
                release,
            ]:
                preparer = serving_deployment_preparer(SystemClock())
                make_application = partial(
                    worker_lifecycle_app,
                    diagnostics=diagnostics,
                    release=int(release),
                    recovery=mode != "hold_claim_response",
                    native_sigterm=mode == "hold_worker_recovery_native_term",
                )
                port = int(requested_port)
                worker_startup = True
            case [
                (
                    "hold_scheduler_poll"
                    | "hold_scheduler_poll_native_term"
                    | "hold_scheduler_poll_forced"
                ) as mode,
                requested_port,
                release,
            ]:
                preparer = serving_deployment_preparer(SystemClock())
                make_application = partial(
                    worker_lifecycle_app,
                    diagnostics=diagnostics,
                    release=int(release),
                    recovery=False,
                    native_sigterm=False,
                    scheduler_http_hold=mode == "hold_scheduler_poll_forced",
                )
                make_scheduler = partial(
                    scheduler_lifecycle_owner,
                    release=int(release),
                    native_sigterm=mode == "hold_scheduler_poll_native_term",
                    forced_owners=(
                        forced_owners if mode == "hold_scheduler_poll_forced" else None
                    ),
                )
                port = int(requested_port)
                worker_startup = True
            case ["buffered_response"]:
                make_application = buffered_response
            case _:
                make_application = held_request_app
        result = serve_sqlite(
            settings,
            port,
            diagnostics.logger(COMPONENT),
            make_application,
            preparer=preparer,
            worker_startup=worker_startup,
            make_scheduler=make_scheduler,
        )
        if sys.argv[1:2] == ["hold_native"]:
            assert not owns_sqlite_descriptor(settings.sqlite_path)
            os.write(1, b"native_sqlite_file_closed\n")
        if sys.argv[1:2] == ["hold_scheduler_poll_forced"]:
            [database] = forced_owners
            assert database.lifecycle is Lifecycle.CLOSED
            assert database.units_in_flight() == 0
            assert database.writer_is_free()
            assert not owns_sqlite_descriptor(settings.sqlite_path)
            os.write(1, b"scheduler_database_settled\n")
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
    stderr: asyncio.StreamReader, event_name: str, *, outcome: str | None = None
) -> dict[object, object]:
    while line := await stderr.readline():
        event: object = json.loads(line)
        if isinstance(event, dict):
            fields = cast("dict[object, object]", event)
            if fields.get("event") == event_name and (
                outcome is None or fields.get("outcome") == outcome
            ):
                return fields
    raise AssertionError(f"the child exited before {event_name}")


if __name__ == "__main__":
    sys.exit(launch())
