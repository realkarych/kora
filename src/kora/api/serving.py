import asyncio
import logging
import signal
import socket
from collections.abc import Callable, Generator
from contextlib import AsyncExitStack, contextmanager
from enum import StrEnum
from types import FrameType

from starlette.types import ASGIApp
from uvicorn import Config, Server
from uvicorn.protocols.http.h11_impl import H11Protocol

from kora.api.exchange import COMPONENT
from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.cel_eval.binding import BindingUnavailable
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
DRAINING: EventSpec = CATALOG.register(
    EventSpec(
        "api.serving_draining",
        COMPONENT,
        Level.INFO,
        "the API listener stopped accepting",
        {},
    )
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
    DOMAIN_CONFIGURATION = "domain_configuration"
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


class _ServingHttpConnection(H11Protocol):
    _released: asyncio.Event

    def connection_made(self, transport: asyncio.Transport) -> None:
        self._released = asyncio.Event()
        super().connection_made(transport)

    def connection_lost(self, exc: Exception | None) -> None:
        super().connection_lost(exc)
        self._released.set()

    async def wait_released(self) -> None:
        await self._released.wait()


class ApiServer(Server):
    log: ComponentLogger
    drain_deadline: float | None
    _loop: asyncio.AbstractEventLoop
    _force_requested: asyncio.Event
    _lifespan_shutdown: asyncio.Task[None] | None

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
                http=_ServingHttpConnection,
                ws="none",
                proxy_headers=False,
                forwarded_allow_ips=[],
                access_log=False,
                log_config=None,
                use_colors=False,
            )
        )
        self.log = log
        self.servers = []
        self._loop = asyncio.get_running_loop()
        self._force_requested = asyncio.Event()
        self.drain_deadline = None
        self._lifespan_shutdown = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        super().handle_exit(sig, frame)
        if self.force_exit:
            self._loop.call_soon_threadsafe(self._force_requested.set)

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

    def disarm_listeners(self) -> None:
        for listener in self.servers:
            for bound_socket in listener.sockets:
                self._loop.remove_reader(bound_socket.fileno())

    async def seal_connections(self) -> tuple[_ServingHttpConnection, ...]:
        self.disarm_listeners()
        accepted: asyncio.Future[None] = self._loop.create_future()

        def enrolled() -> None:
            self.disarm_listeners()
            accepted.set_result(None)

        self._loop.call_soon(enrolled)
        await accepted
        for listener in self.servers:
            listener.close()
        connections: list[_ServingHttpConnection] = []
        for connection in self.server_state.connections:
            if not isinstance(connection, _ServingHttpConnection):
                raise ServingFailure(ServingRefusal.STARTUP_FAILED)
            connections.append(connection)
        return tuple(connections)

    async def wait_for_requests(
        self,
        connections: tuple[_ServingHttpConnection, ...],
        requests: tuple[asyncio.Task[None], ...],
    ) -> None:
        if requests:
            await asyncio.wait(requests)
        for connection in connections:
            await connection.wait_released()

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DEFAULT_CLOSE_DEADLINE_SECONDS
        self.drain_deadline = deadline
        connections = await self.seal_connections()
        self.log.emit(DRAINING)
        for connection in connections:
            connection.shutdown()
        requests = tuple(self.server_state.tasks)
        completed = not connections and not requests
        if not completed:
            settled = asyncio.create_task(self.wait_for_requests(connections, requests))
            forced = asyncio.create_task(self._force_requested.wait())
            try:
                done, _ = await asyncio.wait(
                    (settled, forced),
                    timeout=max(0.0, deadline - loop.time()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                completed = settled in done
            finally:
                settled.cancel()
                forced.cancel()
        if completed and not self.force_exit:
            self._lifespan_shutdown = asyncio.create_task(self.lifespan.shutdown())
            await asyncio.wait(
                (self._lifespan_shutdown,), timeout=max(0.0, deadline - loop.time())
            )

    async def drain(self) -> None:
        self.should_exit = True
        connections = await self.seal_connections()
        for connection in connections:
            connection.transport.abort()
        await self.wait_for_requests(connections, tuple(self.server_state.tasks))
        for listener in self.servers:
            await listener.wait_closed()
        if self._lifespan_shutdown is not None:
            await self._lifespan_shutdown


def refuse(log: ComponentLogger, reason: ServingRefusal, code: int) -> int:
    log.emit(REFUSED, reason=reason.value)
    return code


async def settle_domain(
    database: Database,
    server: ApiServer | None,
    resources: AsyncExitStack,
    deadline: float,
) -> None:
    try:
        async with resources:
            if server is not None:
                await server.drain()
    finally:
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        await database.close_sqlite_serving(remaining)


async def settle_serving(
    database: Database,
    server: ApiServer | None,
    log: ComponentLogger,
    domain: AsyncExitStack | None,
) -> bool:
    loop = asyncio.get_running_loop()
    observed_deadline = None if server is None else server.drain_deadline
    deadline = (
        loop.time() + DEFAULT_CLOSE_DEADLINE_SECONDS
        if observed_deadline is None
        else observed_deadline
    )
    if domain is None:
        owners = {
            asyncio.create_task(
                database.close_sqlite_serving(max(0.0, deadline - loop.time()))
            )
        }
        if server is not None:
            owners.add(asyncio.create_task(server.drain()))
    else:
        owners = {
            asyncio.create_task(settle_domain(database, server, domain, deadline))
        }
    _, pending = await asyncio.wait(owners, timeout=max(0.0, deadline - loop.time()))
    unfinished = bool(pending) or loop.time() >= deadline
    if unfinished:
        log.emit(REFUSED, reason=ServingRefusal.DRAIN_UNFINISHED.value)
    if pending:
        await asyncio.wait(pending)
    for owner in owners:
        owner.result()
    return not unfinished


async def serve_on_loop(
    settings: DatabaseSettings,
    port: int,
    log: ComponentLogger,
    signals: ServingSignals,
    make_application: Callable[[Database], ASGIApp],
    preparer: DeploymentPreparer | None,
) -> int:
    database = Database(settings, observe_sqlite_serving=True)
    domain: AsyncExitStack | None = None
    server: ApiServer | None = None
    failure: ServingRefusal | None = None
    try:
        if not await database_is_at_head(database):
            failure = ServingRefusal.SQLITE_UNPREPARED
        elif signals.received is None:
            if preparer is not None:
                domain = AsyncExitStack()
                try:
                    await domain.enter_async_context(preparer)
                except BindingUnavailable as error:
                    raise ServingFailure(ServingRefusal.DOMAIN_CONFIGURATION) from error
            if signals.received is None:
                server = ApiServer(make_application(database), port, log)
                signals.server = server
                await server.serve()
    except ServingFailure as error:
        failure = error.reason
    except SystemExit:
        failure = ServingRefusal.STARTUP_FAILED
    finally:
        in_time = await settle_serving(database, server, log, domain)
    if not in_time:
        return 70
    if failure is not None:
        return refuse(
            log,
            failure,
            78
            if failure
            in {
                ServingRefusal.SQLITE_UNPREPARED,
                ServingRefusal.DOMAIN_CONFIGURATION,
            }
            else 70,
        )
    log.emit(STOPPED)
    return 130 if signals.received == signal.SIGINT else 0


def serve_sqlite(
    settings: DatabaseSettings,
    port: int,
    log: ComponentLogger,
    make_application: Callable[[Database], ASGIApp],
    *,
    preparer: DeploymentPreparer | None = None,
) -> int:
    if not settings.sqlite_path.is_file():
        return refuse(log, ServingRefusal.SQLITE_UNPREPARED, 78)
    signals = ServingSignals()
    with (
        own_server_logs(),
        signals.own(),
        asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner,
    ):
        return runner.run(
            serve_on_loop(settings, port, log, signals, make_application, preparer)
        )
