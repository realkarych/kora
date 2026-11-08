import asyncio
import enum
import math
import os
import sqlite3
import sys
from collections import OrderedDict
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Generator,
    Iterable,
    Mapping,
)
from contextlib import AsyncExitStack, asynccontextmanager, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, cast
from weakref import ReferenceType, ref

from sqlalchemy import URL, Connection, event, make_url, text
from sqlalchemy.engine.interfaces import DBAPIConnection, DBAPICursor, ExceptionContext
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, Pool, StaticPool
from sqlalchemy.pool.base import ConnectionPoolEntry, PoolProxiedConnection
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.util import await_, greenlet_spawn

from kora.db import CLOSE_TIMED_OUT, DB_COMPONENT, DB_LOGGER_NAME, TRANSACTION_FAILED
from kora.db.maintenance import PostgreSQLDurabilityError as PostgreSQLDurabilityError
from kora.db.maintenance import postgresql_transaction_active
from kora.db.transactions import (
    WriterTransactionError,
    managed_writer,
    raise_private_metadata_error,
)
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
SQLITE_SYNCHRONOUS_FULL: int = 2
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
SQLITE_WRITER_DISPATCH: str = "kora_sqlite_writer_dispatch"
MIGRATION_LOCK_ID: int = 213_0002
MIGRATION_LEASE: str = "kora_migration_lease"
POSTGRESQL_WAL_SETTINGS_QUERY: TextClause = text(
    "SELECT pg_catalog.current_setting('fsync'), "
    "pg_catalog.current_setting('full_page_writes'), "
    "pg_catalog.current_setting('synchronous_commit')"
)
POSTGRESQL_LOCAL_FLUSH_MODES: tuple[str, ...] = (
    "local",
    "remote_write",
    "on",
    "remote_apply",
)

Result = TypeVar("Result")


class DatabaseClosedError(RuntimeError):
    pass


class DatabaseSnapshotFailure(enum.StrEnum):
    DEADLINE = "the database snapshot deadline expired"
    UNAVAILABLE = "the database snapshot is unavailable"


class DatabaseSnapshotRejected(RuntimeError):
    def __init__(self, reason: DatabaseSnapshotFailure) -> None:
        self.reason: DatabaseSnapshotFailure = reason
        super().__init__(reason.value)


class DatabaseWriterFailure(enum.StrEnum):
    DEADLINE = "the database writer admission deadline expired"
    UNAVAILABLE = "database writer admission is unavailable"


class DatabaseWriterRejected(RuntimeError):
    def __init__(self, reason: DatabaseWriterFailure) -> None:
        self.reason: DatabaseWriterFailure = reason
        super().__init__(reason.value)


@dataclass
class _WriterAdmission:
    deadline: float
    handed_off: bool = False


class DatabaseConfigurationError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.variable = DATABASE_URL_VARIABLE
        self.reason = reason


class SQLiteSynchronizationError(RuntimeError):
    pass


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


def verify_full_sync(cursor: DBAPICursor) -> None:
    cursor.execute("PRAGMA main.synchronous")
    if cursor.fetchone() != (SQLITE_SYNCHRONOUS_FULL,):
        raise SQLiteSynchronizationError(
            "SQLite connection requires FULL synchronization"
        )


def apply_full_sync(cursor: DBAPICursor) -> None:
    cursor.execute("PRAGMA main.synchronous=FULL")
    verify_full_sync(cursor)


def verify_macos_fullfsync(cursor: DBAPICursor) -> None:
    cursor.execute("PRAGMA fullfsync")
    if cursor.fetchone() != (1,):
        raise SQLiteSynchronizationError("macOS SQLite connection requires fullfsync")


def apply_platform_fullfsync(cursor: DBAPICursor) -> None:
    if sys.platform == "darwin":
        cursor.execute("PRAGMA fullfsync=ON")
        verify_macos_fullfsync(cursor)


def configure_full_sync_writer(
    raw_connection: DBAPIConnection, record: ConnectionPoolEntry
) -> None:
    with closing(raw_connection.cursor()) as cursor:
        apply_platform_fullfsync(cursor)
        cursor.execute("PRAGMA main.journal_mode=WAL")
        if cursor.fetchone() != ("wal",):
            raise SQLiteSynchronizationError("SQLite writer requires WAL journal mode")
        apply_full_sync(cursor)
        cursor.execute("PRAGMA foreign_keys=ON")


def configure_full_sync_reader(
    raw_connection: DBAPIConnection, record: ConnectionPoolEntry
) -> None:
    with closing(raw_connection.cursor()) as cursor:
        apply_platform_fullfsync(cursor)
        apply_full_sync(cursor)
        cursor.execute("PRAGMA main.journal_mode")
        if cursor.fetchone() != ("wal",):
            raise SQLiteSynchronizationError("SQLite reader requires WAL journal mode")
        for pragma in READER_PRAGMAS:
            cursor.execute(pragma)


def begin_immediately(connection: Connection) -> None:
    with closing(connection.connection.cursor()) as cursor:
        require_sqlite_writer_dispatch(connection)
        cursor.execute(BEGIN_IMMEDIATE)


@contextmanager
def guarded_sqlite_writer_cursor(
    connection: Connection,
) -> Generator[DBAPICursor, None, None]:
    raw_connection = connection.connection
    verified = False
    try:
        with closing(raw_connection.cursor()) as cursor:
            yield cursor
        verified = True
    finally:
        if not verified:
            raw_connection.rollback()


def begin_verified_sqlite_write(connection: Connection) -> None:
    with guarded_sqlite_writer_cursor(connection) as cursor:
        require_sqlite_writer_dispatch(connection)
        cursor.execute(BEGIN_IMMEDIATE)
        cursor.execute("PRAGMA main.journal_mode")
        if cursor.fetchone() != ("wal",):
            raise SQLiteSynchronizationError("SQLite writer requires WAL journal mode")
        verify_full_sync(cursor)
        if sys.platform == "darwin":
            verify_macos_fullfsync(cursor)


def verify_macos_sqlite_commit(connection: Connection) -> None:
    with guarded_sqlite_writer_cursor(connection) as cursor:
        verify_macos_fullfsync(cursor)


@dataclass(frozen=True)
class _SQLiteWriterDispatch:
    owner: asyncio.Task[object]
    cancellation_count: int
    require_entry: Callable[[], None]

    def check(self) -> None:
        self.require_entry()
        if self.owner.cancelling() > self.cancellation_count:
            raise DatabaseSnapshotRejected(DatabaseSnapshotFailure.UNAVAILABLE)


def require_sqlite_writer_dispatch(connection: Connection) -> None:
    dispatch: object = connection.info.get(SQLITE_WRITER_DISPATCH)
    if isinstance(dispatch, _SQLiteWriterDispatch):
        dispatch.check()


def read_sqlite_busy_timeout(cursor: DBAPICursor) -> int:
    cursor.execute("PRAGMA busy_timeout")
    row: object = cursor.fetchone()
    fields = cast(tuple[object, ...], row) if isinstance(row, tuple) else ()
    value = fields[0] if len(fields) == 1 else None
    if type(value) is not int or not 0 <= value <= 2**31 - 1:
        raise SQLiteSynchronizationError("SQLite writer busy timeout is unknown")
    return value


def sqlite_writer_busy_timeout(
    connection: Connection,
) -> tuple[PoolProxiedConnection, int]:
    raw = connection.connection
    if not raw.is_valid:
        raise SQLiteSynchronizationError("SQLite writer checkout is unavailable")
    with closing(raw.cursor()) as cursor:
        return raw, read_sqlite_busy_timeout(cursor)


def set_sqlite_writer_busy_timeout(
    raw: PoolProxiedConnection, milliseconds: int
) -> None:
    if not raw.is_valid:
        raise SQLiteSynchronizationError("SQLite writer checkout is unavailable")
    with closing(raw.cursor()) as cursor:
        cursor.execute(f"PRAGMA busy_timeout={milliseconds}")
        if read_sqlite_busy_timeout(cursor) != milliseconds:
            raise SQLiteSynchronizationError("SQLite writer busy timeout was not set")


def keep_connection_on_cancellation(context: ExceptionContext) -> None:
    if isinstance(context.original_exception, asyncio.CancelledError):
        context.is_disconnect = False


def sqlite_engine(
    settings: DatabaseSettings, writer: bool, *, require_full_sync: bool = False
) -> AsyncEngine:
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
    if require_full_sync:
        configure = configure_full_sync_writer if writer else configure_full_sync_reader
        event.listen(engine.sync_engine, "connect", configure)
    else:
        pragmas = WRITER_PRAGMAS if writer else READER_PRAGMAS
        event.listen(engine.sync_engine, "connect", apply_pragmas(pragmas))
    if writer:
        begin = begin_verified_sqlite_write if require_full_sync else begin_immediately
        event.listen(engine.sync_engine, "begin", begin)
        if require_full_sync and sys.platform == "darwin":
            event.listen(engine.sync_engine, "commit", verify_macos_sqlite_commit)
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
        isolation_level="READ COMMITTED",
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        pool_pre_ping=False,
    )
    event.listen(
        engine.sync_engine, "handle_error", keep_migration_connection_on_cancellation
    )
    return engine


def require_postgresql_wal_settings(observed: tuple[object, ...]) -> None:
    if (
        len(observed) != 3
        or observed[:2] != ("on", "on")
        or observed[2] not in POSTGRESQL_LOCAL_FLUSH_MODES
    ):
        raise PostgreSQLDurabilityError(
            "PostgreSQL writer requires durable WAL settings"
        )


async def verify_postgresql_wal_settings(connection: AsyncConnection) -> None:
    settings = await connection.execute(POSTGRESQL_WAL_SETTINGS_QUERY)
    require_postgresql_wal_settings(tuple(settings.one()))


def verify_postgresql_wal_cursor(cursor: DBAPICursor) -> None:
    cursor.execute(POSTGRESQL_WAL_SETTINGS_QUERY.text)
    observed: Iterable[object] | None = cursor.fetchone()
    require_postgresql_wal_settings(tuple(observed) if observed is not None else ())


def postgresql_autocommit(connection: Connection) -> bool:
    return connection.get_execution_options().get("isolation_level") == "AUTOCOMMIT"


def rollback_postgresql_migration_commit(connection: Connection) -> None:
    try:
        connection.connection.rollback()
        if postgresql_transaction_active(connection):
            raise PostgreSQLDurabilityError(
                "PostgreSQL migration rollback did not settle"
            )
    except BaseException as error:
        connection.invalidate(error)
        raise


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
    return await wait_releasing(task, asyncio.shield(task), release)


async def wait_releasing(
    task: asyncio.Future[Result],
    protected: asyncio.Future[Result],
    release: Callable[[Result], Awaitable[None]],
) -> Result:
    try:
        return await protected
    except asyncio.CancelledError:
        await wait_through_cancellations(task)
        if not task.cancelled() and task.exception() is None:
            release_task = asyncio.ensure_future(release(task.result()))
            await wait_through_cancellations(release_task)
        raise


async def close_connection(connection: AsyncConnection) -> None:
    await connection.close()


async def nothing(result: object) -> None:
    return None


async def complete(pending: Awaitable[Result]) -> Result:
    return await finish_releasing(pending, nothing)


async def start_snapshot_phase(
    operation: Callable[[], Awaitable[Result]],
    release: Callable[[Result], Awaitable[None]],
) -> Result:
    armed = asyncio.Event()
    protected: asyncio.Future[Result] | None = None

    async def start() -> Result:
        await armed.wait()
        if protected is None or protected.cancelled():
            raise DatabaseSnapshotRejected(DatabaseSnapshotFailure.UNAVAILABLE)
        return await operation()

    task = asyncio.create_task(start())
    protected = asyncio.shield(task)
    armed.set()
    return await wait_releasing(task, protected, release)


def require_snapshot_time(deadline: float | None) -> None:
    if deadline is not None and asyncio.get_running_loop().time() >= deadline:
        raise DatabaseSnapshotRejected(DatabaseSnapshotFailure.DEADLINE)


def clear_snapshot_error(error: BaseException) -> None:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None


class _SnapshotAdmission:
    def __init__(self, engine: AsyncEngine, capacity: int) -> None:
        self._pool: ReferenceType[Pool] = ref(engine.pool)
        self._capacity: int = capacity
        self._claims: int = 0
        self._closing: bool = False
        self._verified: bool = True
        self._waiters: OrderedDict[asyncio.Future[None], None] = OrderedDict()

    def _notify(self) -> None:
        if self._waiters and (
            self._closing or not self._verified or self._claims < self._capacity
        ):
            waiter, _ = self._waiters.popitem(last=False)
            waiter.set_result(None)

    def close_admission(self) -> None:
        self._closing = True
        self._notify()

    def fence(self) -> None:
        self._verified = False
        self._claims = 0
        self._notify()

    def reserve(self) -> None:
        if self._verified:
            self._claims += 1

    def _observe(self, engine: AsyncEngine) -> None:
        pool = engine.pool
        if (
            pool is not self._pool()
            or type(pool) is not AsyncAdaptedQueuePool
            or not 0 <= pool.checkedout() <= self._claims
        ):
            self.fence()

    def _release(self, engine: AsyncEngine) -> None:
        if self._verified:
            self._claims -= 1
            self._observe(engine)
            self._notify()

    async def connect_reserved(self, engine: AsyncEngine) -> AsyncConnection:
        try:
            return await engine.connect()
        except PoolTimeoutError as error:
            if error.code == "3o7r":
                self._release(engine)
            else:
                self.fence()
            raise
        except BaseException:
            self.fence()
            raise

    async def release(
        self, connection: AsyncConnection | None, *, verify_generation: bool = False
    ) -> None:
        if connection is not None:
            try:
                try:
                    if verify_generation and connection.engine.pool is not self._pool():
                        await connection.invalidate()
                finally:
                    await connection.close()
            except BaseException:
                self.fence()
                raise
            if connection.closed:
                self._release(connection.engine)
            else:
                self.fence()

    async def release_snapshot(self, connection: AsyncConnection | None) -> None:
        await self.release(connection, verify_generation=True)

    def require_current(self, engine: AsyncEngine, deadline: float) -> None:
        require_snapshot_time(deadline)
        if self._verified:
            self._observe(engine)
        if not self._verified or self._capacity <= 0:
            raise DatabaseSnapshotRejected(DatabaseSnapshotFailure.UNAVAILABLE)

    def _require_start(self, engine: AsyncEngine, deadline: float) -> None:
        self.require_current(engine, deadline)
        if self._closing:
            raise DatabaseSnapshotRejected(DatabaseSnapshotFailure.UNAVAILABLE)

    async def _wait_for_capacity(self) -> None:
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters[waiter] = None
        try:
            await asyncio.shield(waiter)
        finally:
            self._waiters.pop(waiter, None)
            waiter.cancel()

    async def connect_before(
        self, engine: AsyncEngine, deadline: float
    ) -> AsyncConnection:
        async def attempt() -> AsyncConnection | None:
            self._require_start(engine, deadline)
            if self._claims >= self._capacity:
                return None
            self.reserve()
            self._notify()
            return await self.connect_reserved(engine)

        try:
            while True:
                self._require_start(engine, deadline)
                if self._claims >= self._capacity:
                    await self._wait_for_capacity()
                connection = await start_snapshot_phase(attempt, self.release_snapshot)
                if connection is not None:
                    return connection
        finally:
            self._notify()


Unit = Callable[[AsyncConnection], Awaitable[Result]]
SNAPSHOT_NATIVE_ERRORS: tuple[type[Exception], ...] = (
    DatabaseClosedError,
    OSError,
    SQLiteSynchronizationError,
    SQLAlchemyError,
)
WRITER_ENTRY_NATIVE_ERRORS: tuple[type[Exception], ...] = (
    *SNAPSHOT_NATIVE_ERRORS,
    PostgreSQLDurabilityError,
    sqlite3.Error,
    WriterTransactionError,
)


class Database:
    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        require_sqlite_full_sync: bool = False,
        require_postgresql_wal_settings: bool = False,
    ) -> None:
        if require_sqlite_full_sync and settings.dialect is not Dialect.SQLITE:
            raise ValueError("the SQLite writer mode requires SQLite")
        if (
            require_postgresql_wal_settings
            and settings.dialect is not Dialect.POSTGRESQL
        ):
            raise ValueError("the PostgreSQL writer mode requires PostgreSQL")
        self.settings = settings
        self.dialect = settings.dialect
        self._require_postgresql_wal_settings = require_postgresql_wal_settings
        self._writer_lock = asyncio.Lock()
        self._sqlite_writer_usable = True
        if settings.dialect is Dialect.SQLITE:
            create_private_file(settings.sqlite_path)
            self._writer = sqlite_engine(
                settings, writer=True, require_full_sync=require_sqlite_full_sync
            )
            self._reader = sqlite_engine(
                settings, writer=False, require_full_sync=require_sqlite_full_sync
            )
        else:
            self._writer = postgresql_engine(settings)
            self._reader = self._writer
        reader_capacity = (
            SQLITE_READERS
            if self.dialect is Dialect.SQLITE
            else (
                settings.pool_size + settings.max_overflow
                if settings.pool_size > 0 and settings.max_overflow >= 0
                else 0
            )
        )
        self._snapshot_admission: _SnapshotAdmission = _SnapshotAdmission(
            self._reader, reader_capacity
        )
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
    async def transaction(
        self, *, admission_deadline: float | None = None
    ) -> AsyncGenerator[AsyncConnection, None]:
        if admission_deadline is None:
            async with self._lease(transactional=True) as connection:
                with managed_writer(connection):
                    yield connection
            return
        async with self._timed_writer(admission_deadline) as connection:
            yield connection

    @asynccontextmanager
    async def _timed_writer(
        self, deadline: float
    ) -> AsyncGenerator[AsyncConnection, None]:
        if type(deadline) is not float or not math.isfinite(deadline):
            raise_private_metadata_error(
                DatabaseWriterRejected(DatabaseWriterFailure.UNAVAILABLE)
            )
        timer = asyncio.timeout_at(deadline)
        admission = _WriterAdmission(deadline)
        connection = None
        try:
            async with AsyncExitStack() as stack:
                self._require_writer_entry(deadline)
                async with timer:
                    connection = await stack.enter_async_context(
                        self._lease(transactional=True, admission=admission)
                    )
                self._require_writer_entry(deadline)
                with managed_writer(connection):
                    admission.handed_off = True
                    yield connection
            return
        except asyncio.CancelledError as error:
            if admission.handed_off:
                raise
            clear_snapshot_error(error)
            del self, deadline, timer, connection
            raise
        except DatabaseSnapshotRejected as error:
            if admission.handed_off:
                raise
            reason = (
                DatabaseWriterFailure.DEADLINE
                if error.reason is DatabaseSnapshotFailure.DEADLINE
                else DatabaseWriterFailure.UNAVAILABLE
            )
            clear_snapshot_error(error)
        except WRITER_ENTRY_NATIVE_ERRORS as error:
            if admission.handed_off:
                raise
            reason = (
                DatabaseWriterFailure.DEADLINE
                if isinstance(error, TimeoutError) and timer.expired()
                else DatabaseWriterFailure.UNAVAILABLE
            )
            clear_snapshot_error(error)
        del self, deadline, timer, connection
        raise_private_metadata_error(DatabaseWriterRejected(reason))

    @asynccontextmanager
    async def migration_connection(self) -> AsyncGenerator[AsyncConnection, None]:
        if self.dialect is Dialect.SQLITE:
            async with self.transaction() as connection:
                yield connection
            return
        async with self._lease(transactional=False) as connection:
            previous_timeout = str(await connection.scalar(text("SHOW lock_timeout")))
            connection.info[MIGRATION_LEASE] = True
            native = connection.sync_connection
            event.listen(native, "commit", self._migration_commit)
            if self._require_postgresql_wal_settings:
                event.listen(native, "before_cursor_execute", self._migration_dispatch)
            try:
                await complete(self._lock_migration(connection))
                yield connection
            finally:
                if self._require_postgresql_wal_settings:
                    event.remove(
                        native, "before_cursor_execute", self._migration_dispatch
                    )
                event.remove(native, "commit", self._migration_commit)
                await complete(self._release_migration(connection, previous_timeout))

    def _migration_commit(self, connection: Connection) -> None:
        try:
            self._refuse_when_closed()
            if (
                self._require_postgresql_wal_settings
                and not postgresql_autocommit(connection)
                and postgresql_transaction_active(connection)
            ):
                with closing(
                    connection.execute(POSTGRESQL_WAL_SETTINGS_QUERY)
                ) as settings:
                    require_postgresql_wal_settings(tuple(settings.one()))
                self._refuse_when_closed()
        except BaseException:
            if not connection.closed and not connection.invalidated:
                if self._require_postgresql_wal_settings:
                    await_(
                        complete(
                            greenlet_spawn(
                                rollback_postgresql_migration_commit, connection
                            )
                        )
                    )
                else:
                    connection.connection.rollback()
            raise

    def _migration_dispatch(self, connection: Connection, *arguments: object) -> None:
        if postgresql_autocommit(connection):
            self._refuse_when_closed()
            with closing(connection.connection.cursor()) as cursor:
                verify_postgresql_wal_cursor(cursor)
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
        self, transactional: bool, *, admission: _WriterAdmission | None = None
    ) -> AsyncGenerator[AsyncConnection, None]:
        self._enter_unit()
        cancelled_entry = False
        try:
            if self.dialect is Dialect.SQLITE:
                async with self._writer_lock:
                    if not self._sqlite_writer_usable:
                        raise DatabaseClosedError("the database writer is unavailable")
                    async with self._unit(
                        self._writer, transactional, admission=admission
                    ) as connection:
                        yield connection
            else:
                verify_postgresql_writer = (
                    transactional and self._require_postgresql_wal_settings
                )
                async with self._unit(
                    self._writer,
                    transactional,
                    admission=admission,
                    verify_postgresql_writer=verify_postgresql_writer,
                ) as connection:
                    if admission is None and verify_postgresql_writer:
                        await complete(verify_postgresql_wal_settings(connection))
                        self._refuse_when_closed()
                    yield connection
        except asyncio.CancelledError:
            cancelled_entry = admission is not None and not admission.handed_off
            raise
        finally:
            try:
                await self._leave_unit_and_release()
            except WRITER_ENTRY_NATIVE_ERRORS:
                if not cancelled_entry:
                    raise

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
        async with self._read_transaction(None) as connection:
            yield connection

    @asynccontextmanager
    async def _read_transaction(
        self, deadline: float | None
    ) -> AsyncGenerator[AsyncConnection, None]:
        self._enter_unit()
        cancelled = False
        try:
            async with self._unit(self._reader, deadline=deadline) as connection:
                yield connection
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            try:
                await self._leave_unit_and_release()
            except (asyncio.CancelledError, *SNAPSHOT_NATIVE_ERRORS):
                if deadline is None or not cancelled:
                    raise

    @asynccontextmanager
    async def read_snapshot(
        self, *, deadline: float | None = None
    ) -> AsyncGenerator[AsyncConnection, None]:
        if deadline is None:
            async with self._read_snapshot(None) as connection:
                yield connection
            return
        timer = asyncio.timeout_at(deadline)
        connection = None
        try:
            if type(deadline) is not float or not math.isfinite(deadline):
                raise DatabaseSnapshotRejected(DatabaseSnapshotFailure.UNAVAILABLE)
            require_snapshot_time(deadline)
            async with timer:
                try:
                    async with self._read_snapshot(deadline) as connection:
                        yield connection
                except TimeoutError as error:
                    clear_snapshot_error(error)
                    raise DatabaseSnapshotRejected(
                        DatabaseSnapshotFailure.UNAVAILABLE
                    ) from None
            require_snapshot_time(deadline)
            return
        except asyncio.CancelledError as error:
            clear_snapshot_error(error)
            del self, deadline, timer, connection
            raise
        except DatabaseSnapshotRejected as error:
            reason = error.reason
            clear_snapshot_error(error)
        except SNAPSHOT_NATIVE_ERRORS as error:
            reason = (
                DatabaseSnapshotFailure.DEADLINE
                if isinstance(error, TimeoutError) and timer.expired()
                else DatabaseSnapshotFailure.UNAVAILABLE
            )
            clear_snapshot_error(error)
        del self, deadline, timer, connection
        raise_private_metadata_error(DatabaseSnapshotRejected(reason))

    @asynccontextmanager
    async def _read_snapshot(
        self, deadline: float | None
    ) -> AsyncGenerator[AsyncConnection, None]:
        if self.dialect is Dialect.SQLITE:
            setup = text("BEGIN DEFERRED")
        else:
            setup = text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        async with self._read_transaction(deadline) as connection:
            await self._run_snapshot_phase(deadline, lambda: connection.execute(setup))
            self._require_snapshot_current(deadline)
            yield connection

    def _require_snapshot_current(self, deadline: float | None) -> None:
        self._refuse_when_closed()
        if deadline is not None:
            self._snapshot_admission.require_current(self._reader, deadline)

    async def _run_snapshot_phase(
        self, deadline: float | None, operation: Callable[[], Awaitable[Result]]
    ) -> Result:
        if deadline is None:
            return await complete(operation())

        async def checked_phase() -> Result:
            self._require_snapshot_current(deadline)
            return await operation()

        return await start_snapshot_phase(checked_phase, nothing)

    async def _checkout(
        self, engine: AsyncEngine, deadline: float | None
    ) -> AsyncConnection:
        if engine is not self._reader:
            return await finish_releasing(engine.connect(), close_connection)
        if deadline is not None:
            return await self._snapshot_admission.connect_before(engine, deadline)
        self._snapshot_admission.reserve()
        return await finish_releasing(
            self._snapshot_admission.connect_reserved(engine),
            self._snapshot_admission.release,
        )

    def _require_writer_entry(self, deadline: float) -> None:
        require_snapshot_time(deadline)
        if self._lifecycle is not Lifecycle.OPEN or not self._sqlite_writer_usable:
            raise DatabaseSnapshotRejected(DatabaseSnapshotFailure.UNAVAILABLE)
        if self.dialect is Dialect.POSTGRESQL:
            self._snapshot_admission.require_current(self._writer, deadline)

    async def _checkout_writer_before(self, deadline: float) -> AsyncConnection:
        self._require_writer_entry(deadline)
        if self.dialect is Dialect.POSTGRESQL:
            return await self._snapshot_admission.connect_before(self._writer, deadline)

        async def connect() -> AsyncConnection:
            self._require_writer_entry(deadline)
            return await self._writer.connect()

        async def release_late_checkout(connection: AsyncConnection) -> None:
            await self._release_sqlite_writer_entry(connection, cancelled=True)

        return await start_snapshot_phase(connect, release_late_checkout)

    async def _run_writer_entry_phase(
        self, deadline: float, operation: Callable[[], Awaitable[Result]]
    ) -> Result:
        async def checked_phase() -> Result:
            self._require_writer_entry(deadline)
            return await operation()

        return await start_snapshot_phase(checked_phase, nothing)

    async def _prepare_writer_entry(
        self, connection: AsyncConnection, deadline: float
    ) -> None:
        if self.dialect is Dialect.SQLITE:
            await self._begin_sqlite_writer_before(connection, deadline)
        else:
            await self._run_writer_entry_phase(deadline, connection.begin)
            if self._require_postgresql_wal_settings:
                await self._run_writer_entry_phase(
                    deadline, lambda: verify_postgresql_wal_settings(connection)
                )
        self._require_writer_entry(deadline)
        root = connection.get_transaction()
        if root is None or not root.is_active or connection.invalidated:
            raise DatabaseSnapshotRejected(DatabaseSnapshotFailure.UNAVAILABLE)

    async def _begin_sqlite_writer_before(
        self, connection: AsyncConnection, deadline: float
    ) -> None:
        owner = asyncio.current_task()
        if owner is None:
            raise DatabaseSnapshotRejected(DatabaseSnapshotFailure.UNAVAILABLE)
        raw, previous = await self._run_writer_entry_phase(
            deadline, lambda: connection.run_sync(sqlite_writer_busy_timeout)
        )
        ready = False
        cancelled = False
        try:
            await self._run_writer_entry_phase(
                deadline,
                lambda: connection.run_sync(
                    lambda _: set_sqlite_writer_busy_timeout(raw, 0)
                ),
            )
            info = connection.info
            info[SQLITE_WRITER_DISPATCH] = _SQLiteWriterDispatch(
                owner, owner.cancelling(), lambda: self._require_writer_entry(deadline)
            )
            try:
                await self._run_writer_entry_phase(deadline, connection.begin)
            finally:
                info.pop(SQLITE_WRITER_DISPATCH)
            ready = True
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            try:
                await complete(
                    self._restore_sqlite_writer(connection, raw, previous, ready)
                )
            except WRITER_ENTRY_NATIVE_ERRORS:
                if not cancelled:
                    raise

    async def _restore_sqlite_writer(
        self,
        connection: AsyncConnection,
        raw: PoolProxiedConnection,
        previous: int,
        ready: bool,
    ) -> None:
        def restore(_: Connection) -> None:
            if not ready and raw.is_valid:
                raw.rollback()
            set_sqlite_writer_busy_timeout(raw, previous)

        try:
            await connection.run_sync(restore)
        except WRITER_ENTRY_NATIVE_ERRORS:
            try:
                if ready and raw.is_valid:
                    await connection.run_sync(lambda _: raw.rollback())
            finally:
                await self._discard_writer_entry(connection)
            raise

    async def _discard_writer_entry(self, connection: AsyncConnection) -> None:
        try:
            await connection.invalidate()
        except (asyncio.CancelledError, *WRITER_ENTRY_NATIVE_ERRORS):
            if not connection.invalidated:
                if self.dialect is Dialect.POSTGRESQL:
                    self._snapshot_admission.fence()
                else:
                    self._sqlite_writer_usable = False
            raise

    async def _release_sqlite_writer_entry(
        self, connection: AsyncConnection, *, cancelled: bool
    ) -> None:
        try:
            await complete(connection.close())
        except (asyncio.CancelledError, *WRITER_ENTRY_NATIVE_ERRORS):
            try:
                if not connection.closed:
                    await complete(self._discard_writer_entry(connection))
            except (asyncio.CancelledError, *WRITER_ENTRY_NATIVE_ERRORS):
                if not cancelled:
                    raise
            if not cancelled:
                raise

    async def _rollback_writer_entry(
        self, connection: AsyncConnection, cause: BaseException
    ) -> None:
        async def rollback_or_discard() -> None:
            try:
                await connection.rollback()
            except WRITER_ENTRY_NATIVE_ERRORS:
                await self._discard_writer_entry(connection)
                raise

        try:
            await complete(rollback_or_discard())
        except WRITER_ENTRY_NATIVE_ERRORS:
            if not isinstance(cause, asyncio.CancelledError):
                raise

    @asynccontextmanager
    async def _unit(
        self,
        engine: AsyncEngine,
        transactional: bool = True,
        *,
        deadline: float | None = None,
        admission: _WriterAdmission | None = None,
        verify_postgresql_writer: bool = False,
    ) -> AsyncGenerator[AsyncConnection, None]:
        self._refuse_when_closed()
        connection = (
            await self._checkout(engine, deadline)
            if admission is None
            else await self._checkout_writer_before(admission.deadline)
        )
        cancelled = False
        try:
            self._require_snapshot_current(deadline)
            if admission is not None:
                await self._prepare_writer_entry(connection, admission.deadline)
            elif transactional:
                await self._run_snapshot_phase(deadline, connection.begin)
            try:
                yield connection
                self._refuse_when_closed()
                if verify_postgresql_writer:
                    await complete(self._commit_verified_postgresql_writer(connection))
                elif transactional:
                    await self._run_snapshot_phase(deadline, connection.commit)
            except BaseException as error:
                if admission is not None and not admission.handed_off:
                    raise
                await self._rollback_unit(connection, deadline, error)
                self._record_rollback(error)
                raise
        except BaseException as error:
            cancelled = isinstance(error, asyncio.CancelledError)
            if admission is not None and not admission.handed_off:
                await self._rollback_writer_entry(connection, error)
            raise
        finally:
            before_handoff = admission is not None and not admission.handed_off
            if before_handoff and engine is not self._reader:
                await self._release_sqlite_writer_entry(connection, cancelled=cancelled)
            else:
                await self._release_unit(
                    engine,
                    connection,
                    verify_generation=deadline is not None
                    or (admission is not None and engine is self._reader),
                    preserve_cancellation=cancelled
                    and (deadline is not None or before_handoff),
                )
        if deadline is not None:
            self._snapshot_admission.require_current(engine, deadline)

    async def _rollback_unit(
        self,
        connection: AsyncConnection,
        deadline: float | None,
        cause: BaseException,
    ) -> None:
        if deadline is None:
            await complete(connection.rollback())
            return

        async def rollback_snapshot() -> None:
            try:
                await connection.rollback()
            except SNAPSHOT_NATIVE_ERRORS:
                self._snapshot_admission.fence()
                raise

        try:
            await complete(rollback_snapshot())
        except (asyncio.CancelledError, *SNAPSHOT_NATIVE_ERRORS):
            if not isinstance(cause, asyncio.CancelledError):
                raise

    async def _release_unit(
        self,
        engine: AsyncEngine,
        connection: AsyncConnection,
        *,
        verify_generation: bool,
        preserve_cancellation: bool,
    ) -> None:
        try:
            if verify_generation:
                await complete(self._snapshot_admission.release_snapshot(connection))
            elif engine is self._reader:
                await complete(self._snapshot_admission.release(connection))
            else:
                await complete(connection.close())
        except (asyncio.CancelledError, *SNAPSHOT_NATIVE_ERRORS):
            if not preserve_cancellation:
                raise
            if not connection.closed:
                self._snapshot_admission.fence()

    async def _commit_verified_postgresql_writer(
        self, connection: AsyncConnection
    ) -> None:
        await verify_postgresql_wal_settings(connection)
        self._refuse_when_closed()
        await connection.commit()

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
            self._snapshot_admission.close_admission()
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
