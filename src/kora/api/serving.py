import asyncio
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable, Generator
from contextlib import AsyncExitStack, ExitStack, contextmanager, nullcontext
from csv import Error as CsvError
from enum import StrEnum
from functools import partial
from types import FrameType
from typing import Protocol

from starlette.types import ASGIApp
from uvicorn import Config, Server
from uvicorn.lifespan.on import LifespanOn
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
from kora.protocols.scheduler_lifecycle import SchedulerFailure

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
        "the API is draining its owned work",
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
    WORKER_CONFIGURATION = "worker_configuration"
    SCHEDULER_CONFIGURATION = "scheduler_configuration"
    SCHEDULER_FAILED = "scheduler_failed"
    SQLITE_UNPREPARED = "sqlite_unprepared"
    POSTGRESQL_PROFILE_UNAVAILABLE = "postgresql_profile_unavailable"
    POSTGRESQL_BARRIER_UNAVAILABLE = "postgresql_barrier_unavailable"
    POSTGRESQL_ACTIVATION_UNAVAILABLE = "postgresql_activation_unavailable"
    BIND_FAILED = "bind_failed"
    STARTUP_FAILED = "startup_failed"
    DRAIN_UNFINISHED = "drain_unfinished"


class ServingScheduler(Protocol):
    async def recover(self) -> None: ...

    async def run(self) -> None: ...

    def stop(self) -> None: ...


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
def own_signal_wakeup(loop: asyncio.AbstractEventLoop) -> Generator[None]:
    with ExitStack() as cleanup:
        receiver, sender = os.pipe()
        cleanup.callback(os.close, receiver)
        cleanup.callback(os.close, sender)
        os.set_blocking(receiver, False)
        os.set_blocking(sender, False)
        loop.add_reader(receiver, os.read, receiver, 4096)
        cleanup.callback(loop.remove_reader, receiver)
        previous = signal.set_wakeup_fd(sender, warn_on_full_buffer=False)
        cleanup.callback(signal.set_wakeup_fd, previous)
        yield


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


class _StoppedBeforeBind(Exception):
    pass


class _WorkerLifespan(LifespanOn):
    def __init__(
        self,
        config: Config,
        *,
        stopped: Callable[[], bool],
        recover: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(config)
        self._stopped = stopped
        self._recover = recover

    async def startup(self) -> None:
        await super().startup()
        if not self.should_exit:
            try:
                await self._recover()
            except (SchedulerFailure, TimeoutError):
                await self.shutdown()
                raise ServingFailure(ServingRefusal.STARTUP_FAILED) from None
        stopped = self._stopped()
        if stopped and not self.should_exit:
            raise _StoppedBeforeBind()


class _WorkerConfig(Config):
    startup_stopped: Callable[[], bool]
    startup_recovery: Callable[[], Awaitable[None]]

    def load(self) -> None:
        super().load()
        self.lifespan_class = partial(
            _WorkerLifespan, stopped=self.startup_stopped, recover=self.startup_recovery
        )


class ApiServer(Server):
    log: ComponentLogger
    drain_deadline: float | None
    _loop: asyncio.AbstractEventLoop
    _force_requested: asyncio.Event
    _lifespan_shutdown: asyncio.Task[None] | None
    _startup_pending: bool
    _startup_timeout: asyncio.TimerHandle | None
    _drain_unfinished: bool
    _scheduler_task: asyncio.Task[None] | None

    def __init__(
        self,
        app: ASGIApp,
        port: int,
        log: ComponentLogger,
        *,
        worker_startup: bool = False,
        scheduler: ServingScheduler | None = None,
    ) -> None:
        config_type = _WorkerConfig if worker_startup else Config
        config = config_type(
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
        if isinstance(config, _WorkerConfig):
            config.startup_stopped = self._startup_stopped
            config.startup_recovery = self._recover_scheduler
        super().__init__(config)
        self.log = log
        self.servers = []
        self._loop = asyncio.get_running_loop()
        self._force_requested = asyncio.Event()
        self.drain_deadline = None
        self._lifespan_shutdown = None
        self._startup_pending = worker_startup
        self._startup_timeout = None
        self._drain_unfinished = False
        self._scheduler = scheduler
        self._scheduler_task = None

    async def _recover_scheduler(self) -> None:
        if self._scheduler is not None and not self.should_exit:
            await self._scheduler.recover()

    async def _run_scheduler(self, scheduler: ServingScheduler) -> None:
        if self.should_exit:
            return
        try:
            await scheduler.run()
        finally:
            self.should_exit = True

    def _startup_stopped(self) -> bool:
        self._startup_pending = False
        return self.should_exit

    def _watch_startup_drain(self, deadline: float) -> None:
        self.log.emit(DRAINING)
        self._startup_timeout = self._loop.call_at(deadline, self.report_unfinished)

    def report_unfinished(self) -> None:
        if not self._drain_unfinished:
            self._drain_unfinished = True
            self.log.emit(REFUSED, reason=ServingRefusal.DRAIN_UNFINISHED.value)

    def finish_startup_watch(self) -> None:
        if self._startup_timeout is not None:
            self._startup_timeout.cancel()

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        super().handle_exit(sig, frame)
        if self._scheduler is not None:
            self._loop.call_soon_threadsafe(self._scheduler.stop)
        if self._startup_pending and self.drain_deadline is None:
            deadline = self._loop.time() + DEFAULT_CLOSE_DEADLINE_SECONDS
            self.drain_deadline = deadline
            self._loop.call_soon_threadsafe(self._watch_startup_drain, deadline)
        if self.force_exit:
            self._loop.call_soon_threadsafe(self._force_requested.set)

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        if sockets is not None:
            raise ServingFailure(ServingRefusal.STARTUP_FAILED)
        if self.should_exit:
            return
        try:
            await super().startup()
        except _StoppedBeforeBind:
            self._lifespan_shutdown = asyncio.create_task(self.lifespan.shutdown())
            return
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
                if self._scheduler is not None and not self.should_exit:
                    self._scheduler_task = asyncio.create_task(
                        self._run_scheduler(self._scheduler)
                    )
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
        if self._scheduler is not None:
            self._scheduler.stop()
        try:
            await self._drain_http()
        finally:
            if self._scheduler_task is not None:
                await self._scheduler_task

    async def _drain_http(self) -> None:
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
        if server is None:
            log.emit(REFUSED, reason=ServingRefusal.DRAIN_UNFINISHED.value)
        else:
            server.report_unfinished()
    if pending:
        await asyncio.wait(pending)
    for owner in owners:
        owner.result()
    if server is not None:
        server.finish_startup_watch()
    return not unfinished


async def serve_on_loop(
    settings: DatabaseSettings,
    port: int,
    log: ComponentLogger,
    signals: ServingSignals,
    make_application: Callable[[Database], ASGIApp],
    preparer: DeploymentPreparer | None,
    worker_startup: bool = False,
    make_scheduler: Callable[[Database], ServingScheduler] | None = None,
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
                except (
                    BindingUnavailable,
                    CsvError,
                    OSError,
                    UnicodeDecodeError,
                ) as error:
                    raise ServingFailure(ServingRefusal.DOMAIN_CONFIGURATION) from error
            if signals.received is None:
                if make_scheduler is not None and domain is None:
                    domain = AsyncExitStack()
                scheduler = (
                    make_scheduler(database) if make_scheduler is not None else None
                )
                server = ApiServer(
                    make_application(database),
                    port,
                    log,
                    worker_startup=worker_startup,
                    scheduler=scheduler,
                )
                signals.server = server
                if signals.received is None:
                    await server.serve()
    except ServingFailure as error:
        failure = error.reason
    except SystemExit:
        failure = ServingRefusal.STARTUP_FAILED
    finally:
        try:
            in_time = await settle_serving(database, server, log, domain)
        except SchedulerFailure:
            failure = ServingRefusal.SCHEDULER_FAILED
            in_time = True
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
    worker_startup: bool = False,
    make_scheduler: Callable[[Database], ServingScheduler] | None = None,
) -> int:
    if not settings.sqlite_path.is_file():
        return refuse(log, ServingRefusal.SQLITE_UNPREPARED, 78)
    if make_scheduler is not None and not worker_startup:
        raise ValueError("scheduler serving requires Worker startup ownership")
    signals = ServingSignals()
    with (
        own_server_logs(),
        signals.own(),
        asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner,
        own_signal_wakeup(runner.get_loop()) if worker_startup else nullcontext(),
    ):
        return runner.run(
            serve_on_loop(
                settings,
                port,
                log,
                signals,
                make_application,
                preparer,
                worker_startup,
                make_scheduler,
            )
        )
