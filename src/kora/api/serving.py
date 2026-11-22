import asyncio
import logging
import signal
import socket
from collections.abc import Callable, Generator
from contextlib import contextmanager
from enum import StrEnum
from types import FrameType

from starlette.types import ASGIApp
from uvicorn import Config, Server

from kora.api.exchange import COMPONENT
from kora.db.connection import (
    DEFAULT_CLOSE_DEADLINE_SECONDS,
    Database,
    DatabaseSettings,
)
from kora.db.migrations import database_is_at_head
from kora.observability import CATALOG, ComponentLogger, EventSpec, FieldKind, Level

LOOPBACK: str = "127.0.0.1"
SERVER_LOGGERS: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "uvicorn.asgi",
)
BOUND: EventSpec = CATALOG.register(
    EventSpec(
        "api.serving_bound",
        COMPONENT,
        Level.INFO,
        "the API listener is open",
        {"port": FieldKind.INTEGER},
    )
)
STOPPED: EventSpec = CATALOG.register(
    EventSpec("api.serving_stopped", COMPONENT, Level.INFO, "the API owner settled", {})
)
REFUSED: EventSpec = CATALOG.register(
    EventSpec(
        "api.serving_refused",
        COMPONENT,
        Level.ERROR,
        "the API owner refused",
        {"reason": FieldKind.STRING},
    )
)


class ServingRefusal(StrEnum):
    DATABASE_CONFIGURATION = "database_configuration"
    SQLITE_UNPREPARED = "sqlite_unprepared"
    POSTGRESQL_PROFILE_UNAVAILABLE = "postgresql_profile_unavailable"
    POSTGRESQL_BARRIER_UNAVAILABLE = "postgresql_barrier_unavailable"
    POSTGRESQL_ACTIVATION_UNAVAILABLE = "postgresql_activation_unavailable"
    BIND_FAILED = "bind_failed"
    STARTUP_FAILED = "startup_failed"
    DRAIN_UNFINISHED = "drain_unfinished"


class ServingFailure(Exception):
    def __init__(self, reason: ServingRefusal) -> None:
        self.reason = reason
        super().__init__(reason.value)


class ServingSignals:
    server: Server | None
    received: int | None

    def __init__(self) -> None:
        self.server = None
        self.received = None

    def stop(self, signum: int, frame: FrameType | None) -> None:
        if self.received is None or signum == signal.SIGINT:
            self.received = signum
        if self.server is not None:
            self.server.should_exit = True

    @contextmanager
    def own(self) -> Generator[None]:
        interrupt = signal.signal(signal.SIGINT, self.stop)
        try:
            terminate = signal.signal(signal.SIGTERM, self.stop)
            try:
                yield
            finally:
                signal.signal(signal.SIGTERM, terminate)
        finally:
            signal.signal(signal.SIGINT, interrupt)


@contextmanager
def own_server_logs() -> Generator[None]:
    loggers = tuple(logging.getLogger(name) for name in SERVER_LOGGERS)
    disabled = tuple(logger.disabled for logger in loggers)
    try:
        for logger in loggers:
            logger.disabled = True
        yield
    finally:
        for logger, previous in zip(loggers, disabled, strict=True):
            logger.disabled = previous


class ApiServer(Server):
    log: ComponentLogger

    def __init__(self, app: ASGIApp, port: int, log: ComponentLogger) -> None:
        super().__init__(
            Config(
                app,
                host=LOOPBACK,
                port=port,
                workers=1,
                reload=False,
                lifespan="on",
                interface="asgi3",
                http="h11",
                ws="none",
                proxy_headers=False,
                forwarded_allow_ips=[],
                access_log=False,
                log_config=None,
                use_colors=False,
                timeout_graceful_shutdown=int(DEFAULT_CLOSE_DEADLINE_SECONDS),
            )
        )
        self.log = log
        self.servers = []

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        if sockets is not None:
            raise ServingFailure(ServingRefusal.STARTUP_FAILED)
        if self.should_exit:
            return
        try:
            await super().startup()
        except SystemExit as error:
            reason = (
                ServingRefusal.STARTUP_FAILED
                if self.lifespan.should_exit
                else ServingRefusal.BIND_FAILED
            )
            raise ServingFailure(reason) from error
        listeners = tuple(
            listener for server in self.servers for listener in server.sockets
        )
        if len(listeners) != 1:
            raise ServingFailure(ServingRefusal.BIND_FAILED)
        address: object = listeners[0].getsockname()
        match address:
            case ("127.0.0.1", int(port)) if 1 <= port <= 65535:
                self.log.emit(BOUND, port=port)
            case _:
                raise ServingFailure(ServingRefusal.BIND_FAILED)

    async def drain(self, deadline: float) -> bool:
        self.should_exit = True
        for listener in self.servers:
            listener.close()
        for connection in tuple(self.server_state.connections):
            connection.shutdown()
        tasks = tuple(self.server_state.tasks)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        try:
            async with asyncio.timeout_at(deadline):
                if tasks:
                    await asyncio.wait(tasks)
                for listener in self.servers:
                    await listener.wait_closed()
        except TimeoutError:
            return False
        return not self.server_state.connections and not self.server_state.tasks


def refuse(log: ComponentLogger, reason: ServingRefusal, code: int) -> int:
    log.emit(REFUSED, reason=reason.value)
    return code


async def serve_on_loop(
    settings: DatabaseSettings,
    port: int,
    log: ComponentLogger,
    signals: ServingSignals,
    make_application: Callable[[Database], ASGIApp],
) -> int:
    database = Database(settings)
    server: ApiServer | None = None
    failure: ServingRefusal | None = None
    try:
        if not await database_is_at_head(database):
            failure = ServingRefusal.SQLITE_UNPREPARED
        elif signals.received is None:
            server = ApiServer(make_application(database), port, log)
            signals.server = server
            await server.serve()
    except ServingFailure as error:
        failure = error.reason
    except SystemExit:
        failure = ServingRefusal.STARTUP_FAILED
    finally:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DEFAULT_CLOSE_DEADLINE_SECONDS
        server_closed = False
        try:
            server_closed = server is None or await server.drain(deadline)
        finally:
            database_closed = await database.close_sqlite_serving(
                max(0.0, deadline - loop.time())
            )
            if not server_closed or not database_closed:
                log.emit(REFUSED, reason=ServingRefusal.DRAIN_UNFINISHED.value)
    if not server_closed or not database_closed:
        return 70
    if failure is not None:
        return refuse(
            log, failure, 78 if failure is ServingRefusal.SQLITE_UNPREPARED else 70
        )
    log.emit(STOPPED)
    return 130 if signals.received == signal.SIGINT else 0


def serve_sqlite(
    settings: DatabaseSettings,
    port: int,
    log: ComponentLogger,
    make_application: Callable[[Database], ASGIApp],
) -> int:
    if not settings.sqlite_path.is_file():
        return refuse(log, ServingRefusal.SQLITE_UNPREPARED, 78)
    signals = ServingSignals()
    with (
        own_server_logs(),
        signals.own(),
        asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner,
    ):
        return runner.run(serve_on_loop(settings, port, log, signals, make_application))
