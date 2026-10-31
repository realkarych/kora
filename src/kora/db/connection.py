import asyncio
import enum
import os
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from sqlalchemy import URL, Connection, event, make_url, text
from sqlalchemy.engine.interfaces import DBAPIConnection, ExceptionContext
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, StaticPool
from sqlalchemy.pool.base import ConnectionPoolEntry

from kora.db import CLOSE_TIMED_OUT, DB_COMPONENT, DB_LOGGER_NAME, TRANSACTION_FAILED
from kora.db.types import POSTGRESQL, SQLITE
from kora.observability import ComponentLogger, current_diagnostics

DATABASE_URL_VARIABLE: str = "KORA_DATABASE_URL"
SQLITE_DRIVER: str = "sqlite+aiosqlite"
POSTGRESQL_DRIVER: str = "postgresql+asyncpg"
ACCEPTED_FORMS: str = (
    "expected sqlite+aiosqlite:///<path> or "
    "postgresql+asyncpg://<user>:<password>@<host>:<port>/<database>"
)
MEMORY_DATABASE: str = ":memory:"
PRIVATE_FILE_MODE: int = 0o600
SQLITE_BUSY_TIMEOUT_SECONDS: float = 5.0
SQLITE_READERS: int = 4
DEFAULT_POOL_SIZE: int = 5
DEFAULT_MAX_OVERFLOW: int = 5
DEFAULT_POOL_TIMEOUT_SECONDS: float = 5.0
DEFAULT_CLOSE_DEADLINE_SECONDS: float = 5.0
WRITER_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
)
READER_PRAGMAS: tuple[str, ...] = (
    "PRAGMA foreign_keys=ON",
    "PRAGMA query_only=ON",
)
BEGIN_IMMEDIATE: str = "BEGIN IMMEDIATE"
MIGRATION_LOCK_ID: int = 213_0002
MIGRATION_LEASE: str = "kora_migration_lease"

Result = TypeVar("Result")


class DatabaseClosedError(RuntimeError):
    pass


class DatabaseConfigurationError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.variable = DATABASE_URL_VARIABLE
        self.reason = reason


class Dialect(enum.StrEnum):
    SQLITE = SQLITE
    POSTGRESQL = POSTGRESQL


class Lifecycle(enum.Enum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True)
class DatabaseSettings:
    url: URL
    dialect: Dialect
    pool_size: int = DEFAULT_POOL_SIZE
    max_overflow: int = DEFAULT_MAX_OVERFLOW
    pool_timeout_seconds: float = DEFAULT_POOL_TIMEOUT_SECONDS

    @property
    def sqlite_path(self) -> Path:
        if self.dialect is not Dialect.SQLITE or self.url.database is None:
            raise DatabaseConfigurationError("the database is not a SQLite file")
        return Path(self.url.database)


def parse_database_url(text: str) -> DatabaseSettings:
    try:
        url = make_url(text)
    except (ArgumentError, ValueError) as error:
        raise DatabaseConfigurationError(ACCEPTED_FORMS) from error
    if url.drivername == SQLITE_DRIVER:
        if not url.database or url.database == MEMORY_DATABASE:
            raise DatabaseConfigurationError("a SQLite database is a file path")
        if url.host or url.username or url.password or url.port or url.query:
            raise DatabaseConfigurationError(ACCEPTED_FORMS)
        return DatabaseSettings(url=url, dialect=Dialect.SQLITE)
    if url.drivername == POSTGRESQL_DRIVER:
        complete = (
            bool(url.username)
            and bool(url.password)
            and bool(url.host)
            and url.port is not None
            and bool(url.database)
        )
        if not complete:
            raise DatabaseConfigurationError(
                "a PostgreSQL URL names its user, password, host, port and database"
            )
        return DatabaseSettings(url=url, dialect=Dialect.POSTGRESQL)
    raise DatabaseConfigurationError(ACCEPTED_FORMS)


def read_database_settings(environ: Mapping[str, str]) -> DatabaseSettings:
    text = environ.get(DATABASE_URL_VARIABLE)
    if text is None or not text.strip():
        raise DatabaseConfigurationError("the variable is required")
    return parse_database_url(text.strip())


def create_private_file(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE)
    os.close(descriptor)


def apply_pragmas(
    pragmas: tuple[str, ...],
) -> Callable[[DBAPIConnection, ConnectionPoolEntry], None]:
    def on_connect(
        raw_connection: DBAPIConnection, record: ConnectionPoolEntry
    ) -> None:
        cursor = raw_connection.cursor()
        for pragma in pragmas:
            cursor.execute(pragma)
        cursor.close()

    return on_connect


def begin_immediately(connection: Connection) -> None:
    cursor = connection.connection.cursor()
    cursor.execute(BEGIN_IMMEDIATE)
    cursor.close()


def keep_connection_on_cancellation(context: ExceptionContext) -> None:
    if isinstance(context.original_exception, asyncio.CancelledError):
        context.is_disconnect = False


def sqlite_engine(settings: DatabaseSettings, writer: bool) -> AsyncEngine:
    connect_args = {"timeout": SQLITE_BUSY_TIMEOUT_SECONDS, "isolation_level": None}
    if writer:
        engine = create_async_engine(
            settings.url, poolclass=StaticPool, connect_args=connect_args
        )
    else:
        engine = create_async_engine(
            settings.url,
            poolclass=AsyncAdaptedQueuePool,
            pool_size=SQLITE_READERS,
            max_overflow=0,
            pool_timeout=settings.pool_timeout_seconds,
            connect_args=connect_args,
        )
    pragmas = WRITER_PRAGMAS if writer else READER_PRAGMAS
    event.listen(engine.sync_engine, "connect", apply_pragmas(pragmas))
    if writer:
        event.listen(engine.sync_engine, "begin", begin_immediately)
        event.listen(
            engine.sync_engine, "handle_error", keep_connection_on_cancellation
        )
    return engine


def keep_migration_connection_on_cancellation(context: ExceptionContext) -> None:
    if context.connection is not None and context.connection.info.get(MIGRATION_LEASE):
        keep_connection_on_cancellation(context)


def postgresql_engine(settings: DatabaseSettings) -> AsyncEngine:
    engine = create_async_engine(
        settings.url,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        pool_pre_ping=False,
    )
    event.listen(
        engine.sync_engine, "handle_error", keep_migration_connection_on_cancellation
    )
    return engine


async def wait_through_cancellations(task: asyncio.Future[Result]) -> None:
    while not task.done():
        try:
            await asyncio.shield(asyncio.wait({task}))
        except asyncio.CancelledError:
            continue
    if not task.cancelled():
        task.exception()


async def finish_releasing(
    pending: Awaitable[Result], release: Callable[[Result], Awaitable[None]]
) -> Result:
    task = asyncio.ensure_future(pending)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await wait_through_cancellations(task)
        if task.exception() is None:
            release_task = asyncio.ensure_future(release(task.result()))
            await wait_through_cancellations(release_task)
        raise


async def close_connection(connection: AsyncConnection) -> None:
    await connection.close()


async def nothing(result: object) -> None:
    return None


async def complete(pending: Awaitable[Result]) -> Result:
    return await finish_releasing(pending, nothing)


Unit = Callable[[AsyncConnection], Awaitable[Result]]


class Database:
    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings
        self.dialect = settings.dialect
        self._writer_lock = asyncio.Lock()
        if settings.dialect is Dialect.SQLITE:
            create_private_file(settings.sqlite_path)
            self._writer = sqlite_engine(settings, writer=True)
            self._reader = sqlite_engine(settings, writer=False)
        else:
            self._writer = postgresql_engine(settings)
            self._reader = self._writer
        self._lifecycle = Lifecycle.OPEN
        self._closing: asyncio.Future[None] | None = None
        self._disposed = False
        self._in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def writer_engine(self) -> AsyncEngine:
        return self._writer

    @property
    def reader_engine(self) -> AsyncEngine:
        return self._reader

    @property
    def lifecycle(self) -> Lifecycle:
        return self._lifecycle

    def writer_is_free(self) -> bool:
        return not self._writer_lock.locked()

    def units_in_flight(self) -> int:
        return self._in_flight

    def _enter_unit(self) -> None:
        if self._lifecycle is not Lifecycle.OPEN:
            raise DatabaseClosedError("the database is closed")
        self._in_flight += 1
        self._idle.clear()

    def _leave_unit(self) -> bool:
        self._in_flight -= 1
        if self._in_flight > 0:
            return False
        self._idle.set()
        return self._lifecycle is Lifecycle.CLOSED

    async def _leave_unit_and_release(self) -> None:
        if self._leave_unit():
            await complete(self._dispose())

    def _refuse_when_closed(self) -> None:
        if self._lifecycle is Lifecycle.CLOSED:
            raise DatabaseClosedError("the database closed before the unit finished")

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[AsyncConnection, None]:
        async with self._lease(transactional=True) as connection:
            yield connection

    @asynccontextmanager
    async def migration_connection(self) -> AsyncGenerator[AsyncConnection, None]:
        if self.dialect is Dialect.SQLITE:
            async with self.transaction() as connection:
                yield connection
            return
        async with self._lease(transactional=False) as connection:
            previous_timeout = str(await connection.scalar(text("SHOW lock_timeout")))
            connection.info[MIGRATION_LEASE] = True
            event.listen(connection.sync_connection, "commit", self._migration_commit)
            try:
                await complete(self._lock_migration(connection))
                yield connection
            finally:
                event.remove(
                    connection.sync_connection, "commit", self._migration_commit
                )
                await complete(self._release_migration(connection, previous_timeout))

    def _migration_commit(self, connection: Connection) -> None:
        if self._lifecycle is Lifecycle.CLOSED:
            connection.connection.rollback()
        self._refuse_when_closed()

    async def _lock_migration(self, connection: AsyncConnection) -> None:
        await connection.execute(text("SELECT set_config('lock_timeout', '5s', false)"))
        await connection.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_ID}
        )
        await connection.commit()

    async def _release_migration(
        self, connection: AsyncConnection, previous_timeout: str
    ) -> None:
        await connection.rollback()
        if connection.invalidated:
            return
        await connection.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_ID}
        )
        await connection.execute(
            text("SELECT set_config('lock_timeout', :value, false)"),
            {"value": previous_timeout},
        )
        await connection.commit()
        connection.info.pop(MIGRATION_LEASE, None)

    @asynccontextmanager
    async def _lease(
        self, transactional: bool
    ) -> AsyncGenerator[AsyncConnection, None]:
        self._enter_unit()
        try:
            if self.dialect is Dialect.SQLITE:
                async with self._writer_lock:
                    async with self._unit(self._writer, transactional) as connection:
                        yield connection
            else:
                async with self._unit(self._writer, transactional) as connection:
                    yield connection
        finally:
            await self._leave_unit_and_release()

    async def run(self, unit: Unit[Result]) -> Result:
        async def execute() -> Result:
            async with self.transaction() as connection:
                return await unit(connection)

        return await complete(execute())

    async def read(self, unit: Unit[Result]) -> Result:
        async def execute() -> Result:
            async with self.read_transaction() as connection:
                return await unit(connection)

        return await complete(execute())

    @asynccontextmanager
    async def read_transaction(self) -> AsyncGenerator[AsyncConnection, None]:
        self._enter_unit()
        try:
            async with self._unit(self._reader) as connection:
                yield connection
        finally:
            await self._leave_unit_and_release()

    @asynccontextmanager
    async def _unit(
        self, engine: AsyncEngine, transactional: bool = True
    ) -> AsyncGenerator[AsyncConnection, None]:
        self._refuse_when_closed()
        connection = await finish_releasing(engine.connect(), close_connection)
        try:
            self._refuse_when_closed()
            if transactional:
                await complete(connection.begin())
            try:
                yield connection
                self._refuse_when_closed()
                if transactional:
                    await complete(connection.commit())
            except BaseException as error:
                await complete(connection.rollback())
                self._record_rollback(error)
                raise
        finally:
            await complete(connection.close())

    def _record_rollback(self, error: BaseException) -> None:
        if isinstance(error, asyncio.CancelledError):
            return
        logger = self._logger()
        if logger is not None:
            logger.emit(TRANSACTION_FAILED, dialect=str(self.dialect), exception=error)

    def _logger(self) -> ComponentLogger | None:
        diagnostics = current_diagnostics()
        if diagnostics is None:
            return None
        return diagnostics.logger(DB_COMPONENT, name=DB_LOGGER_NAME)

    async def close(
        self, deadline_seconds: float = DEFAULT_CLOSE_DEADLINE_SECONDS
    ) -> None:
        if self._closing is None:
            self._lifecycle = Lifecycle.CLOSING
            self._closing = asyncio.ensure_future(self._close(deadline_seconds))
            self._closing.add_done_callback(self._mark_closed)
        await asyncio.shield(self._closing)

    def _mark_closed(self, closing: asyncio.Future[None]) -> None:
        self._lifecycle = Lifecycle.CLOSED

    async def _close(self, deadline_seconds: float) -> None:
        try:
            await asyncio.wait_for(self._idle.wait(), deadline_seconds)
        except TimeoutError:
            if self._in_flight > 0:
                self._record_close_timed_out()
        finally:
            self._lifecycle = Lifecycle.CLOSED
            if self._in_flight == 0:
                await complete(self._dispose())

    def _record_close_timed_out(self) -> None:
        logger = self._logger()
        if logger is not None:
            logger.emit(
                CLOSE_TIMED_OUT,
                dialect=str(self.dialect),
                units_in_flight=self._in_flight,
            )

    async def _dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        try:
            await self._writer.dispose()
        finally:
            if self._reader is not self._writer:
                await self._reader.dispose()
