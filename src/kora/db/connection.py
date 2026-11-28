from __future__ import annotations

import asyncio
import enum
import fcntl
import math
import os
import sqlite3
import stat
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
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    closing,
    contextmanager,
)
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import TYPE_CHECKING, ParamSpec, Protocol, TypeVar, cast, overload
from weakref import ReferenceType, ref

import aiosqlite
from sqlalchemy import URL, Connection, event, make_url, text
from sqlalchemy.dialects.sqlite.aiosqlite import AsyncAdapt_aiosqlite_dbapi
from sqlalchemy.engine.interfaces import (
    DBAPIConnection,
    DBAPICursor,
    ExceptionContext,
    ExecutionContext,
)
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

from kora.db import (
    CLOSE_TIMED_OUT,
    CUSTODY_UNFINISHED,
    DB_COMPONENT,
    DB_LOGGER_NAME,
    TRANSACTION_FAILED,
    CustodyUnavailable,
    custody_log,
)
from kora.db.completion import complete as complete
from kora.db.completion import finish_releasing as finish_releasing
from kora.db.completion import nothing as nothing
from kora.db.completion import wait_releasing
from kora.db.maintenance import (
    NATIVE_READ_RETIRE,
    NativeReadExpired,
    NativeReadGuard,
    NativeReadUnavailable,
    handle_native_read_error,
    postgresql_transaction_active,
    sqlite_checkout_worker,
)
from kora.db.maintenance import PostgreSQLDurabilityError as PostgreSQLDurabilityError
from kora.db.run_event_maintenance_admission import (
    POSITION_AUTHORITY,
    POSITION_TARGET,
    POSITION_VERIFYING,
    SQLITE_FRESH_EMPTY,
    Admission,
    PositionMaintenanceRefused,
    earlier_position_target,
    position_migration_authority,
    sqlite_has_sidecars,
    sqlite_snapshot,
)
from kora.db.transactions import (
    WriterTransactionError,
    managed_writer,
    raise_private_metadata_error,
    writer_transaction,
)
from kora.db.types import POSTGRESQL, SQLITE, SQLITE_SYNCHRONOUS_FULL
from kora.observability import ComponentLogger, current_diagnostics
from kora.sqlite.driver import Connection as NativeConnection
from kora.sqlite.driver import IsolatedMarker, NativeRetirement, driver_version

if TYPE_CHECKING:
    from kora.db.custody_journal import CustodyJournalLease, CustodySQLiteInstallation
    from kora.db.custody_models import CustodyFileAnchor
    from kora.db.custody_sqlite import SQLiteWorkerOriginal
    from kora.db.custody_transaction import OriginalCustodyTransaction
    from kora.db.custody_writer import (
        LocalSQLiteWriterLoan,
        LocalSQLiteWriterScope,
        SQLitePhysicalWriter,
    )

DATABASE_URL_VARIABLE: str = "KORA_DATABASE_URL"
SQLITE_DRIVER: str = "sqlite+aiosqlite"
POSTGRESQL_DRIVER: str = "postgresql+asyncpg"
ACCEPTED_FORMS: str = (
    "expected sqlite+aiosqlite:///<path> or "
    "postgresql+asyncpg://<user>:<password>@<host>:<port>/<database>"
)
MEMORY_DATABASE: str = ":memory:"
SQLITE_BUSY_TIMEOUT_SECONDS: float = 5.0
SQLITE_PREPARATION: ContextVar[Path | None] = ContextVar(
    "sqlite_preparation", default=None
)
SQLITE_MIGRATION_ADMISSION: ContextVar[
    tuple[Callable[[int, int], bool], bool] | None
] = ContextVar("sqlite_migration_admission", default=None)
SQLITE_READERS: int = 4
AIOSQLITE_ITER_CHUNK_SIZE: int = 64
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
MIGRATION_LOCK_TIMEOUT: str = "5s"
POSTGRESQL_MIGRATION_TIMEOUT_QUERY: TextClause = text(
    "SELECT pg_catalog.set_config("
    "'lock_timeout', CAST(:value AS pg_catalog.text), CAST(:local AS pg_catalog.bool))"
)
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
DriverArgs = ParamSpec("DriverArgs")


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


@dataclass
class _SnapshotOutcome:
    failure: DatabaseSnapshotFailure | None = None


class DatabaseConfigurationError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.variable = DATABASE_URL_VARIABLE
        self.reason = reason


class SQLiteSynchronizationError(RuntimeError):
    pass


class PostgresDriver(Protocol):
    PostgresError: type[Exception]


class PostgresAdapter(Protocol):
    asyncpg: PostgresDriver


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


if TYPE_CHECKING:

    class _SQLiteWorker(aiosqlite.Connection):
        async def _execute(
            self,
            work: Callable[DriverArgs, Result],
            *args: DriverArgs.args,
            **kwargs: DriverArgs.kwargs,
        ) -> Result: ...
else:
    _SQLiteWorker = aiosqlite.Connection


class SqliteDriverConnection(_SQLiteWorker):
    def __init__(
        self, opened: NativeConnection | Callable[[], NativeConnection]
    ) -> None:
        def connect() -> sqlite3.Connection:
            return cast(sqlite3.Connection, opened() if callable(opened) else opened)

        super().__init__(connect, AIOSQLITE_ITER_CHUNK_SIZE)
        self._thread.daemon = True


class _GuardedSqliteDriverConnection(SqliteDriverConnection):
    async def close(self) -> None:
        await complete(self._close_and_join())

    async def _close_and_join(self) -> None:
        try:
            await super().close()
        finally:
            if self._thread.ident is not None:
                await asyncio.to_thread(self._thread.join)


@dataclass(frozen=True)
class IsolatedMetadataMain:
    path: Path
    anchor: CustodyFileAnchor
    directory: int
    require_live: Callable[[], None]

    def admit_main(self, descriptor: int, empty: int) -> bool:
        from kora.db.custody_native import file_anchor, require_native_file

        self.require_live()
        info = require_native_file(descriptor, directory=False)
        if (
            not empty
            or info.st_size
            or file_anchor(info, self.path) != self.anchor
            or fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDWR
        ):
            raise CustodyUnavailable
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                os.stat(
                    self.path.name + suffix,
                    dir_fd=self.directory,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            raise CustodyUnavailable
        return True

    def admit_journal(self, descriptor: int) -> bool:
        from kora.db.custody_native import require_native_file

        self.require_live()
        name = self.path.name + "-journal"
        info = require_native_file(
            descriptor, directory=False, device=os.fstat(self.directory).st_dev
        )
        named = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
            raise CustodyUnavailable
        return True

    def open(self, attempt: NativeConnection) -> NativeConnection:
        self.require_live()
        if self.path.name != "main.db":
            raise CustodyUnavailable
        try:
            attempt.open(
                self.path,
                False,
                0,
                self.admit_main,
                custody=True,
                directory=self.directory,
                journal=self.admit_journal,
                _marker=IsolatedMarker.METADATA,
            )
        except BaseException:
            attempt.close()
            raise
        return attempt

    def mark(self, attempt: NativeConnection) -> None:
        self.require_live()
        for statement in (
            "PRAGMA page_size=4096",
            "PRAGMA journal_mode=DELETE",
            "PRAGMA synchronous=EXTRA",
        ):
            attempt.execute(statement).close()
        self.require_live()
        attempt.commit_isolated_marker()


@dataclass(frozen=True)
class _GuardedSqliteReader:
    path: Path
    require_live: Callable[[], None]

    def before_setup(
        self, raw_connection: DBAPIConnection, record: ConnectionPoolEntry
    ) -> None:
        self.require_live()

    def before_statement(
        self,
        connection: Connection,
        cursor: DBAPICursor,
        statement: str,
        parameters: object,
        context: ExecutionContext | None,
        executemany: bool,
    ) -> None:
        self.require_live()


class _SqliteServingLifetime:
    def __init__(self) -> None:
        self._guard = Lock()
        self._native_open: int = 0
        self._loop = asyncio.get_running_loop()
        self._native_closed = asyncio.Event()
        self.disposed = asyncio.Event()

    def opened(self) -> None:
        with self._guard:
            self._native_open += 1

    def closed(self) -> None:
        with self._guard:
            self._native_open -= 1
            if self._native_open == 0:
                self._loop.call_soon_threadsafe(self._native_closed.set)

    def _all_native_closed(self) -> bool:
        with self._guard:
            return self._native_open == 0

    async def wait_settled(self) -> None:
        await self.disposed.wait()
        while not self._all_native_closed():
            await self._native_closed.wait()
            self._native_closed.clear()


def sqlite_empty_admitted(path: Path, descriptor: int) -> bool:
    if SQLITE_PREPARATION.get() != path:
        return False
    main = path.lstat()
    opened = os.fstat(descriptor)
    if opened.st_size or (main.st_dev, main.st_ino, main.st_size) != (
        opened.st_dev,
        opened.st_ino,
        0,
    ):
        return False
    for ending in ("-wal", "-shm", "-journal"):
        try:
            path.with_name(path.name + ending).lstat()
        except FileNotFoundError:
            continue
        return False
    return True


def sqlite_driver_module() -> AsyncAdapt_aiosqlite_dbapi:
    native = AsyncAdapt_aiosqlite_dbapi(aiosqlite, sqlite3)
    try:
        version = driver_version()
    except sqlite3.OperationalError as error:
        raise DatabaseConfigurationError(
            "the installed SQLite driver is unavailable"
        ) from error
    vars(native).update(
        sqlite_version=version, sqlite_version_info=tuple(map(int, version.split(".")))
    )
    return native


def open_sqlite_file(
    path: Path,
    slots: BoundedSemaphore,
    lifetime: _SqliteServingLifetime | None = None,
    *,
    guarded_reader: _GuardedSqliteReader | None = None,
    custody: SQLitePhysicalWriter | None = None,
    sqlite_admit: Admission | None = None,
) -> NativeConnection:
    driver_version()
    if guarded_reader is not None:
        guarded_reader.require_live()
    NativeRetirement.drain(path)
    reserved = slots.acquire(blocking=False)
    if custody is not None and not reserved:
        raise CustodyUnavailable

    def release() -> None:
        slots.release()
        if lifetime is not None:
            lifetime.closed()

    def retire(owned: NativeConnection) -> None:
        if reserved:
            if owned.reused_main():
                release()
            else:

                def released() -> None:
                    release()
                    if custody is not None:
                        custody.observe_retirement(owned)

                NativeRetirement.retain(path, owned, released)

    def admit(descriptor: int, empty: int) -> bool:
        if guarded_reader is not None:
            guarded_reader.require_live()
        if custody is not None:
            return custody.admit(descriptor, empty)
        migration = SQLITE_MIGRATION_ADMISSION.get()
        if migration is not None:
            return migration[0](descriptor, empty)
        if sqlite_admit is not None:
            return sqlite_admit(descriptor, empty)
        return not empty or sqlite_empty_admitted(path, descriptor)

    try:
        connection = (
            NativeConnection(retire) if custody is None else custody.allocate(retire)
        )
    except BaseException:
        if reserved:
            slots.release()
        raise
    if reserved and lifetime is not None:
        lifetime.opened()
    try:
        prepare = custody is None and reserved and SQLITE_PREPARATION.get() == path
        migration = SQLITE_MIGRATION_ADMISSION.get()
        exclusive = migration is not None and migration[1]
        native_path = path.resolve(strict=True) if exclusive else path
        if prepare:
            path.parent.mkdir(parents=True, exist_ok=True)
        connection.open(
            native_path,
            guarded_reader is not None,
            int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000),
            admit,
            prepare=prepare,
            pool=id(slots)
            if guarded_reader is None and custody is None and not exclusive
            else 0,
            reuse_only=not reserved,
            custody=custody is not None,
            snapshot="exclusive" if exclusive else None,
        )
        if exclusive and (
            sqlite_has_sidecars(path)
            or not sqlite_snapshot(
                path,
                admit,
                lambda native: bool(native.scalar(SQLITE_FRESH_EMPTY)),
                owner=connection,
            )
        ):
            raise PositionMaintenanceRefused
        if reserved and connection.reused_main():
            reserved = False
            release()
    except BaseException:
        connection.close()
        raise
    return connection


def sqlite_connector(
    path: Path,
    capacity: int,
    lifetime: _SqliteServingLifetime | None = None,
    *,
    guarded_reader: _GuardedSqliteReader | None = None,
    custody: SQLitePhysicalWriter | None = None,
    sqlite_admit: Admission | None = None,
) -> Callable[[], Awaitable[aiosqlite.Connection]]:
    slots = BoundedSemaphore(capacity)
    if custody is not None:
        return custody.connector(path, slots)

    async def connect() -> aiosqlite.Connection:
        async def acquire() -> _GuardedSqliteDriverConnection:
            opened = await asyncio.to_thread(
                open_sqlite_file,
                path,
                slots,
                lifetime,
                guarded_reader=guarded_reader,
                sqlite_admit=sqlite_admit,
            )
            driver = _GuardedSqliteDriverConnection(opened)
            try:
                await driver
                if guarded_reader is not None:
                    guarded_reader.require_live()
            except BaseException:
                try:
                    await driver.close()
                finally:
                    await asyncio.to_thread(opened.close)
                raise
            return driver

        return await finish_releasing(acquire(), _GuardedSqliteDriverConnection.close)

    return connect


def retire_guarded_sqlite_reader(
    connection: DBAPIConnection | None, record: ConnectionPoolEntry
) -> None:
    record.close()


def sqlite_engine(
    settings: DatabaseSettings,
    writer: bool,
    *,
    require_full_sync: bool = False,
    serving_lifetime: _SqliteServingLifetime | None = None,
    guarded_reader: _GuardedSqliteReader | None = None,
    custody: SQLitePhysicalWriter | None = None,
    sqlite_admit: Admission | None = None,
) -> AsyncEngine:
    capacity = 1 if writer or guarded_reader is not None else SQLITE_READERS
    connector = sqlite_connector(
        settings.sqlite_path.absolute(),
        capacity,
        serving_lifetime,
        guarded_reader=guarded_reader,
        custody=custody,
        sqlite_admit=sqlite_admit,
    )
    module = sqlite_driver_module()
    if writer:
        engine = create_async_engine(
            settings.url, poolclass=StaticPool, async_creator=connector, module=module
        )
    else:
        engine = create_async_engine(
            settings.url,
            poolclass=AsyncAdaptedQueuePool,
            pool_size=capacity,
            max_overflow=0,
            pool_timeout=settings.pool_timeout_seconds,
            async_creator=connector,
            module=module,
        )
    if custody is not None:
        event.listen(engine.pool, "checkin", custody.checkin)
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
    else:
        event.listen(engine.sync_engine, "handle_error", handle_native_read_error)
    if guarded_reader is not None:
        event.listen(engine.pool, "connect", guarded_reader.before_setup, insert=True)
        event.listen(engine.pool, "checkin", retire_guarded_sqlite_reader)
        event.listen(
            engine.sync_engine, "before_cursor_execute", guarded_reader.before_statement
        )
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
    event.listen(engine.sync_engine, "handle_error", handle_native_read_error)
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


async def close_connection(connection: AsyncConnection) -> None:
    await connection.close()


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


class LocalSQLiteWriterEntry:
    __slots__ = ()


@dataclass(frozen=True)
class LocalSQLiteWriterUnit:
    entry: LocalSQLiteWriterEntry
    token_id: str
    next_token_id: str | None = None


@dataclass
class LocalSQLiteCheckout:
    connection: AsyncConnection | None = None
    worker: SQLiteWorkerOriginal | None = None
    original: OriginalCustodyTransaction | None = None
    attempted: bool = False


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
    @classmethod
    def guarded_sqlite_reader(
        cls, path: Path, require_live: Callable[[], None]
    ) -> Database:
        if not path.is_absolute() or ".." in path.parts:
            raise DatabaseConfigurationError(
                "the protected SQLite path must be canonical"
            )
        settings = DatabaseSettings(
            URL.create(SQLITE_DRIVER, database=str(path)), Dialect.SQLITE
        )
        return cls(
            settings,
            observe_sqlite_serving=True,
            _guarded_reader=_GuardedSqliteReader(path, require_live),
        )

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        require_sqlite_full_sync: bool = False,
        require_postgresql_wal_settings: bool = False,
        observe_sqlite_serving: bool = False,
        _guarded_reader: _GuardedSqliteReader | None = None,
        _local_sqlite_writer: LocalSQLiteWriterScope | None = None,
        sqlite_admit: Admission | None = None,
    ) -> None:
        if _local_sqlite_writer is not None and (
            settings.dialect is not Dialect.SQLITE
            or not require_sqlite_full_sync
            or observe_sqlite_serving
            or _guarded_reader is not None
        ):
            raise CustodyUnavailable
        self._local_sqlite_writer = _local_sqlite_writer
        if require_sqlite_full_sync and settings.dialect is not Dialect.SQLITE:
            raise ValueError("the SQLite writer mode requires SQLite")
        if (
            require_postgresql_wal_settings
            and settings.dialect is not Dialect.POSTGRESQL
        ):
            raise ValueError("the PostgreSQL writer mode requires PostgreSQL")
        self.settings = settings
        self.dialect = settings.dialect
        if observe_sqlite_serving and self.dialect is not Dialect.SQLITE:
            raise DatabaseConfigurationError("the serving observer requires SQLite")
        self._sqlite_serving = (
            _SqliteServingLifetime() if observe_sqlite_serving else None
        )
        self._require_postgresql_wal_settings = require_postgresql_wal_settings
        self._guarded_reader = _guarded_reader
        self._writer_lock = asyncio.Lock()
        self._sqlite_writer_usable = _guarded_reader is None
        self._writer: AsyncEngine | None
        if _guarded_reader is not None:
            if (
                self._sqlite_serving is None
                or require_sqlite_full_sync
                or settings.sqlite_path != _guarded_reader.path
            ):
                raise DatabaseConfigurationError(
                    "the protected SQLite reader is unavailable"
                )
            self._writer = None
            self._reader = sqlite_engine(
                settings,
                writer=False,
                serving_lifetime=self._sqlite_serving,
                guarded_reader=_guarded_reader,
            )
            reader_capacity = 1
        elif settings.dialect is Dialect.SQLITE:
            self._writer = sqlite_engine(
                settings,
                writer=True,
                require_full_sync=require_sqlite_full_sync,
                serving_lifetime=self._sqlite_serving,
                sqlite_admit=sqlite_admit,
                custody=None
                if _local_sqlite_writer is None
                else _local_sqlite_writer.physical,
            )
            self._reader = sqlite_engine(
                settings,
                writer=False,
                require_full_sync=require_sqlite_full_sync,
                serving_lifetime=self._sqlite_serving,
                sqlite_admit=sqlite_admit,
            )
            reader_capacity = SQLITE_READERS
        else:
            self._writer = postgresql_engine(settings)
            self._reader = self._writer
            reader_capacity = (
                settings.pool_size + settings.max_overflow
                if settings.pool_size > 0 and settings.max_overflow >= 0
                else 0
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
        self._require_ordinary_database()
        if self._writer is None:
            raise DatabaseClosedError("the database writer is unavailable")
        return self._writer

    @property
    def reader_engine(self) -> AsyncEngine:
        self._require_ordinary_database()
        return self._reader

    def _require_ordinary_database(self) -> None:
        if self._local_sqlite_writer is not None:
            raise CustodyUnavailable

    def postgresql_driver_error(self) -> type[Exception] | None:
        if self.dialect is not Dialect.POSTGRESQL:
            return None
        return cast(
            "PostgresAdapter", self.writer_engine.dialect.loaded_dbapi
        ).asyncpg.PostgresError

    @property
    def lifecycle(self) -> Lifecycle:
        return self._lifecycle

    def writer_is_free(self) -> bool:
        return self._writer is not None and not self._writer_lock.locked()

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

    @overload
    def transaction(
        self, *, admission_deadline: float | None = None
    ) -> AbstractAsyncContextManager[AsyncConnection]: ...

    @overload
    def transaction(
        self,
        *,
        _local_sqlite_writer: LocalSQLiteWriterEntry,
        admission_deadline: float | None = None,
    ) -> AbstractAsyncContextManager[LocalSQLiteWriterLoan]: ...

    @asynccontextmanager
    async def transaction(
        self,
        *,
        admission_deadline: float | None = None,
        _local_sqlite_writer: LocalSQLiteWriterEntry | None = None,
    ) -> AsyncGenerator[AsyncConnection | LocalSQLiteWriterLoan, None]:
        if _local_sqlite_writer is not None:
            async with AsyncExitStack() as stack:
                try:
                    admitted = await stack.enter_async_context(
                        self._admit_local_sqlite_writer(
                            _local_sqlite_writer, admission_deadline
                        )
                    )
                    loan = await stack.enter_async_context(
                        self._local_sqlite_transaction(*admitted, admission_deadline)
                    )
                except (CustodyUnavailable, *WRITER_ENTRY_NATIVE_ERRORS):
                    if admission_deadline is None:
                        raise
                    reason = (
                        DatabaseWriterFailure.DEADLINE
                        if type(admission_deadline) is float
                        and math.isfinite(admission_deadline)
                        and asyncio.get_running_loop().time() >= admission_deadline
                        else DatabaseWriterFailure.UNAVAILABLE
                    )
                    raise_private_metadata_error(DatabaseWriterRejected(reason))
                yield loan
            return
        self._require_ordinary_database()
        if admission_deadline is None:
            async with self._lease(transactional=True) as connection:
                with managed_writer(connection):
                    yield connection
            return
        async with self._timed_writer(admission_deadline) as connection:
            yield connection

    @asynccontextmanager
    async def _admit_local_sqlite_writer(
        self, entry: LocalSQLiteWriterEntry, deadline: float | None
    ) -> AsyncGenerator[
        tuple[LocalSQLiteWriterScope, CustodyJournalLease, CustodySQLiteInstallation]
    ]:
        scope = self._local_sqlite_writer
        if (
            scope is None
            or entry is not scope.entry
            or self._lifecycle is not Lifecycle.OPEN
        ):
            raise CustodyUnavailable
        from kora.db.custody_journal import journal_lease
        from kora.db.custody_sqlite import require_sqlite_anchor

        self._require_local_sqlite_entry(deadline)
        async with journal_lease(
            scope.root,
            ordinary=(scope.lifetime, scope.participant),
            sqlite_accounting=(self._enter_unit, self._leave_unit_and_release),
        ) as journal:
            self._require_local_sqlite_entry(deadline)
            installation = await journal.require_sqlite_installation(
                scope.lifetime, scope.participant
            )
            require_sqlite_anchor(
                self.settings.sqlite_path.absolute(), installation.anchor
            )
            self._require_local_sqlite_entry(deadline)
            yield scope, journal, installation

    def _require_local_sqlite_entry(self, deadline: float | None) -> None:
        if self._lifecycle is not Lifecycle.OPEN or not self._sqlite_writer_usable:
            raise CustodyUnavailable
        if deadline is not None:
            if type(deadline) is not float or not math.isfinite(deadline):
                raise DatabaseWriterRejected(DatabaseWriterFailure.UNAVAILABLE)
            if asyncio.get_running_loop().time() >= deadline:
                raise DatabaseWriterRejected(DatabaseWriterFailure.DEADLINE)

    @asynccontextmanager
    async def _local_sqlite_transaction(
        self,
        scope: LocalSQLiteWriterScope,
        journal: CustodyJournalLease,
        installation: CustodySQLiteInstallation,
        deadline: float | None,
    ) -> AsyncGenerator[LocalSQLiteWriterLoan]:
        from kora.db.custody_models import CustodyIssuerKind
        from kora.db.custody_writer import LocalSQLiteWriterLoan

        async with self._writer_lock:
            self._require_local_sqlite_entry(deadline)
            scope.root.require_open_latch()
            checkout = LocalSQLiteCheckout()
            try:
                timer = asyncio.timeout_at(deadline)
                try:
                    async with timer:
                        await self._open_local_sqlite_checkout(
                            checkout, scope, journal, installation, deadline
                        )
                except (
                    DatabaseSnapshotRejected,
                    TimeoutError,
                    *WRITER_ENTRY_NATIVE_ERRORS,
                ) as error:
                    if deadline is None:
                        raise
                    reason = (
                        DatabaseWriterFailure.DEADLINE
                        if timer.expired()
                        or isinstance(error, DatabaseSnapshotRejected)
                        and error.reason is DatabaseSnapshotFailure.DEADLINE
                        else DatabaseWriterFailure.UNAVAILABLE
                    )
                    raise_private_metadata_error(DatabaseWriterRejected(reason))
                connection, worker, original = (
                    checkout.connection,
                    checkout.worker,
                    checkout.original,
                )
                if connection is None or worker is None or original is None:
                    raise CustodyUnavailable
                with managed_writer(connection):
                    with LocalSQLiteWriterLoan.lend(
                        writer_transaction(connection), worker
                    ) as loan:
                        yield loan
                self._refuse_when_closed()
                participant = scope.participant
                prepared = await journal.prepare_original_commit(
                    original,
                    journal_id=participant.journal_id,
                    generation=participant.generation,
                    previous=installation.frontier,
                    issuer_kind=CustodyIssuerKind.ORDINARY,
                )
                await journal.commit_prepared(original, prepared)
            finally:
                await self._finish_local_sqlite_checkout(checkout, scope, journal)

    async def _open_local_sqlite_checkout(
        self,
        checkout: LocalSQLiteCheckout,
        scope: LocalSQLiteWriterScope,
        journal: CustodyJournalLease,
        installation: CustodySQLiteInstallation,
        deadline: float | None,
    ) -> None:
        writer = self._writer
        if writer is None:
            raise CustodyUnavailable

        async def acquire() -> AsyncConnection:
            self._require_local_sqlite_entry(deadline)
            scope.root.require_open_latch()
            journal.own_sqlite_checkout(checkout)
            checkout.attempted = True
            scope.physical.returned = None
            scope.physical.installation = installation
            connection = await writer.connect()
            checkout.connection = connection
            return connection

        connection = await start_snapshot_phase(acquire, nothing)
        self._require_local_sqlite_entry(deadline)
        if deadline is None:
            await complete(connection.begin())
        else:
            await self._prepare_writer_entry(connection, deadline)
        await complete(
            self._bind_local_sqlite_checkout(checkout, connection, scope, installation)
        )
        self._require_local_sqlite_entry(deadline)

    async def _bind_local_sqlite_checkout(
        self,
        checkout: LocalSQLiteCheckout,
        connection: AsyncConnection,
        scope: LocalSQLiteWriterScope,
        installation: CustodySQLiteInstallation,
    ) -> None:
        from kora.db.custody_models import custody_counter
        from kora.db.custody_sqlite import SQLiteWorkerOriginal
        from kora.db.custody_transaction import OriginalCustodyTransaction
        from kora.db.custody_writer import (
            LOCAL_SQLITE_NATIVE_SEQUENCE,
            CustodyDriverConnection,
        )

        driver = await sqlite_checkout_worker(connection)
        if (
            not isinstance(driver, CustodyDriverConnection)
            or driver is not scope.physical.driver
        ):
            raise CustodyUnavailable
        participant = scope.participant
        root = await driver.bind_custody_root(installation, participant.journal_id)
        worker = SQLiteWorkerOriginal(driver, root)
        checkout.worker = worker
        checkout.original = await OriginalCustodyTransaction.capture(
            connection,
            metadata_id=installation.metadata_id,
            issuer_sequence=participant.issuer_sequence,
            connection_sequence=custody_counter(next(LOCAL_SQLITE_NATIVE_SEQUENCE)),
            transaction_sequence=custody_counter(next(LOCAL_SQLITE_NATIVE_SEQUENCE)),
            sqlite=worker,
        )

    async def _close_local_sqlite_checkout(
        self, checkout: LocalSQLiteCheckout, physical: SQLitePhysicalWriter
    ) -> None:
        original = checkout.original
        if original is not None:
            if original.outcome is None:
                await original.rollback()
            await original.close_sqlite()
        else:
            worker = checkout.worker
            if worker is not None:
                await worker.rollback()
                await worker.require_native_closed()
            if checkout.connection is None:
                if self._writer is not None:
                    await self._writer.dispose()
                driver = physical.driver
                if driver is not None and not driver.retired:
                    await driver.close()
            else:
                if self._lifecycle is not Lifecycle.OPEN:
                    await checkout.connection.invalidate()
                await checkout.connection.close()

    async def _finish_local_sqlite_checkout(
        self,
        checkout: LocalSQLiteCheckout,
        scope: LocalSQLiteWriterScope,
        journal: CustodyJournalLease,
    ) -> None:
        if not checkout.attempted:
            return

        async def close_and_settle() -> None:
            if journal.defer_sqlite_checkout(schedule_checkout):
                return
            physical = scope.physical
            await self._close_local_sqlite_checkout(checkout, physical)
            if not physical.released:
                driver = physical.driver
                if driver is not None:
                    driver.after_retirement(schedule_retirement)
                raise CustodyUnavailable
            await settle()

        async def settle() -> None:
            await self._confirm_local_sqlite_release(checkout, journal)

        async def resume(work: Callable[[], Awaitable[None]]) -> None:
            try:
                await work()
            except (CustodyUnavailable, *WRITER_ENTRY_NATIVE_ERRORS):
                custody_log(CUSTODY_UNFINISHED, "ordinary_admission")
            finally:
                journal.resume_close()

        def schedule_checkout() -> None:
            asyncio.create_task(resume(close_and_settle))

        def schedule_retirement() -> None:
            asyncio.create_task(resume(settle))

        await complete(close_and_settle())

    async def _confirm_local_sqlite_release(
        self, checkout: LocalSQLiteCheckout, journal: CustodyJournalLease
    ) -> None:
        from kora.db.custody_models import CustodyNativeOutcome

        original = checkout.original
        if original is not None:
            original.confirm_sqlite_release()
            if original.outcome is CustodyNativeOutcome.UNKNOWN:
                raise CustodyUnavailable
        await self._settle_local_sqlite_checkout(checkout, journal)

    async def _settle_local_sqlite_checkout(
        self, checkout: LocalSQLiteCheckout, journal: CustodyJournalLease
    ) -> None:
        original = checkout.original
        try:
            if original is not None and await journal.has_sqlite_preparation(original):
                await journal.settle_original(original)
        finally:
            journal.release_sqlite_checkout(checkout)

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
    async def migration_connection(
        self,
        *,
        target: str = "head",
        sqlite_admit: Callable[[int, int], bool] | None = None,
        sqlite_existing_empty: bool = False,
    ) -> AsyncGenerator[AsyncConnection, None]:
        if self.writer_engine.dialect.name == SQLITE:
            path = self.settings.sqlite_path.absolute()
            positioned = not earlier_position_target(target)
            opened = False

            def admit(descriptor: int, empty: int) -> bool:
                nonlocal opened
                opened = (
                    bool(empty) and sqlite_empty_admitted(path, descriptor)
                    if sqlite_admit is None
                    else sqlite_admit(descriptor, empty)
                )
                return opened

            if positioned and sqlite_admit is None:
                try:
                    existing = path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if existing.st_size or not stat.S_ISREG(existing.st_mode):
                        raise PositionMaintenanceRefused
            preparation = SQLITE_PREPARATION.set(path if sqlite_admit is None else None)
            admission = SQLITE_MIGRATION_ADMISSION.set(
                (admit, sqlite_existing_empty) if positioned else None
            )
            try:
                async with self.transaction() as connection:
                    if positioned and not opened:
                        raise PositionMaintenanceRefused
                    connection.info[POSITION_TARGET] = target
                    try:
                        yield connection
                    finally:
                        connection.info.pop(POSITION_TARGET, None)
            finally:
                SQLITE_MIGRATION_ADMISSION.reset(admission)
                SQLITE_PREPARATION.reset(preparation)
            return
        async with self._lease(transactional=False) as connection:
            previous_timeout = str(await connection.scalar(text("SHOW lock_timeout")))
            connection.info[MIGRATION_LEASE] = True
            connection.info[POSITION_TARGET] = target
            native = connection.sync_connection
            event.listen(native, "commit", self._migration_commit)
            event.listen(native, "before_cursor_execute", self._migration_dispatch)
            try:
                await complete(self._lock_migration(connection))
                yield connection
            finally:
                connection.info.pop(POSITION_TARGET, None)
                event.remove(native, "before_cursor_execute", self._migration_dispatch)
                event.remove(native, "commit", self._migration_commit)
                await complete(self._release_migration(connection, previous_timeout))

    def _migration_commit(self, connection: Connection) -> None:
        try:
            self._refuse_when_closed()
            if POSITION_AUTHORITY in connection.info and postgresql_transaction_active(
                connection
            ):
                position_migration_authority(connection)
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
        if POSITION_VERIFYING in connection.info:
            return
        if POSITION_AUTHORITY in connection.info:
            position_migration_authority(connection)
        if self._require_postgresql_wal_settings and postgresql_autocommit(connection):
            self._refuse_when_closed()
            with closing(connection.connection.cursor()) as cursor:
                verify_postgresql_wal_cursor(cursor)
            self._refuse_when_closed()

    async def _lock_migration(self, connection: AsyncConnection) -> None:
        await connection.execute(
            POSTGRESQL_MIGRATION_TIMEOUT_QUERY,
            {"value": MIGRATION_LOCK_TIMEOUT, "local": False},
        )
        await connection.execute(
            text("SELECT pg_catalog.pg_advisory_lock(CAST(:key AS pg_catalog.int8))"),
            {"key": MIGRATION_LOCK_ID},
        )
        await connection.commit()

    async def _release_migration(
        self, connection: AsyncConnection, previous_timeout: str
    ) -> None:
        await connection.rollback()
        if connection.invalidated:
            return
        await connection.execute(
            text("SELECT pg_catalog.pg_advisory_unlock(CAST(:key AS pg_catalog.int8))"),
            {"key": MIGRATION_LOCK_ID},
        )
        await connection.execute(
            POSTGRESQL_MIGRATION_TIMEOUT_QUERY,
            {"value": previous_timeout, "local": False},
        )
        await connection.commit()
        connection.info.pop(MIGRATION_LEASE, None)

    @asynccontextmanager
    async def _lease(
        self, transactional: bool, *, admission: _WriterAdmission | None = None
    ) -> AsyncGenerator[AsyncConnection, None]:
        writer = self.writer_engine
        self._enter_unit()
        cancelled_entry = False
        try:
            if self.dialect is Dialect.SQLITE:
                async with self._writer_lock:
                    if not self._sqlite_writer_usable:
                        raise DatabaseClosedError("the database writer is unavailable")
                    async with self._unit(
                        writer, transactional, admission=admission
                    ) as connection:
                        yield connection
            else:
                verify_postgresql_writer = (
                    transactional and self._require_postgresql_wal_settings
                )
                async with self._unit(
                    writer,
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

    @overload
    async def run(self, unit: Unit[Result]) -> Result: ...

    @overload
    async def run(self, unit: LocalSQLiteWriterUnit) -> int: ...

    async def run(self, unit: Unit[Result] | LocalSQLiteWriterUnit) -> Result | int:
        if isinstance(unit, LocalSQLiteWriterUnit):
            return await self._run_local_sqlite_unit(unit)
        self._require_ordinary_database()

        async def execute() -> Result:
            async with self.transaction() as connection:
                return await unit(connection)

        return await complete(execute())

    async def _run_local_sqlite_unit(self, unit: LocalSQLiteWriterUnit) -> int:
        async with self._admit_local_sqlite_writer(unit.entry, None) as admitted:

            async def execute() -> int:
                async with self._local_sqlite_transaction(*admitted, None) as loan:
                    return await loan.revoke_token(unit.token_id, unit.next_token_id)

            return await complete(execute())

    async def read(self, unit: Unit[Result]) -> Result:
        async def execute() -> Result:
            async with self.read_transaction() as connection:
                return await unit(connection)

        return await complete(execute())

    @asynccontextmanager
    async def read_transaction(self) -> AsyncGenerator[AsyncConnection, None]:
        self._require_ordinary_database()
        async with self._read_transaction(None) as connection:
            yield connection

    @asynccontextmanager
    async def _read_transaction(
        self, deadline: float | None, *, snapshot: bool = False
    ) -> AsyncGenerator[AsyncConnection, None]:
        guarded_reader = self._guarded_reader
        if guarded_reader is not None:
            guarded_reader.require_live()
        self._enter_unit()
        cancelled = False
        try:
            async with self._unit(
                self._reader, deadline=deadline, snapshot=snapshot
            ) as connection:
                if self.dialect is Dialect.POSTGRESQL and not snapshot:
                    await complete(
                        connection.execute(text("SET TRANSACTION READ ONLY"))
                    )
                    self._refuse_when_closed()
                yield connection
            if guarded_reader is not None:
                guarded_reader.require_live()
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            try:
                await self._leave_unit_and_release()
            except (asyncio.CancelledError, *SNAPSHOT_NATIVE_ERRORS):
                if not snapshot or not cancelled:
                    raise

    @asynccontextmanager
    async def read_snapshot(
        self, *, deadline: float | None = None
    ) -> AsyncGenerator[AsyncConnection, None]:
        self._require_ordinary_database()
        connection = None
        if deadline is None:
            try:
                async with self._read_snapshot(None) as connection:
                    yield connection
            except asyncio.CancelledError as error:
                clear_snapshot_error(error)
                del self, connection
                raise
            return
        timer = asyncio.timeout_at(deadline)
        outcome = _SnapshotOutcome()
        try:
            if type(deadline) is not float or not math.isfinite(deadline):
                raise DatabaseSnapshotRejected(DatabaseSnapshotFailure.UNAVAILABLE)
            require_snapshot_time(deadline)
            async with timer:
                try:
                    async with self._read_snapshot(deadline, outcome) as connection:
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
            del self, deadline, timer, connection, outcome
            raise
        except DatabaseSnapshotRejected as error:
            reason = error.reason
            clear_snapshot_error(error)
        except SNAPSHOT_NATIVE_ERRORS as error:
            reason = outcome.failure or (
                DatabaseSnapshotFailure.DEADLINE
                if isinstance(error, TimeoutError) and timer.expired()
                else DatabaseSnapshotFailure.UNAVAILABLE
            )
            clear_snapshot_error(error)
        del self, deadline, timer, connection, outcome
        raise_private_metadata_error(DatabaseSnapshotRejected(reason))

    @asynccontextmanager
    async def _read_snapshot(
        self, deadline: float | None, outcome: _SnapshotOutcome | None = None
    ) -> AsyncGenerator[AsyncConnection, None]:
        if self.dialect is Dialect.SQLITE:
            setup = text("BEGIN DEFERRED")
        else:
            setup = text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        async with self._read_transaction(deadline, snapshot=True) as connection:
            await self._run_snapshot_phase(deadline, lambda: connection.execute(setup))
            self._require_snapshot_current(deadline)
            if deadline is None:
                yield connection
            else:
                async with self._native_snapshot(connection, deadline, outcome):
                    yield connection

    @asynccontextmanager
    async def _native_snapshot(
        self,
        connection: AsyncConnection,
        deadline: float,
        outcome: _SnapshotOutcome | None,
    ) -> AsyncGenerator[None, None]:
        guard = NativeReadGuard(connection, deadline)
        cancelled = False
        try:
            try:
                await self._run_snapshot_phase(deadline, guard.install)
                yield
            except asyncio.CancelledError:
                cancelled = True
                try:
                    await complete(self._interrupt_native_snapshot(guard))
                except (asyncio.CancelledError, DatabaseSnapshotRejected):
                    pass
                raise
            except NativeReadExpired:
                if outcome is not None:
                    outcome.failure = DatabaseSnapshotFailure.DEADLINE
                raise DatabaseSnapshotRejected(
                    DatabaseSnapshotFailure.DEADLINE
                ) from None
            except (NativeReadUnavailable, *SNAPSHOT_NATIVE_ERRORS) as error:
                reason = (
                    DatabaseSnapshotFailure.DEADLINE
                    if guard.expired_error(error)
                    else DatabaseSnapshotFailure.UNAVAILABLE
                )
                if outcome is not None:
                    outcome.failure = reason
                raise DatabaseSnapshotRejected(reason) from None
        finally:
            try:
                await complete(self._close_native_snapshot(guard))
            except (asyncio.CancelledError, DatabaseSnapshotRejected):
                if not cancelled:
                    raise

    async def _interrupt_native_snapshot(self, guard: NativeReadGuard) -> None:
        try:
            await guard.interrupt()
        except guard.native_errors:
            self._snapshot_admission.fence()
            raise DatabaseSnapshotRejected(
                DatabaseSnapshotFailure.UNAVAILABLE
            ) from None

    async def _close_native_snapshot(self, guard: NativeReadGuard) -> None:
        try:
            await guard.close()
        except (NativeReadUnavailable, *guard.native_errors):
            self._snapshot_admission.fence()
            try:
                await guard.retire()
            except guard.native_errors:
                raise DatabaseSnapshotRejected(
                    DatabaseSnapshotFailure.UNAVAILABLE
                ) from None
            raise DatabaseSnapshotRejected(
                DatabaseSnapshotFailure.UNAVAILABLE
            ) from None

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
            self._snapshot_admission.require_current(self.writer_engine, deadline)

    async def _checkout_writer_before(self, deadline: float) -> AsyncConnection:
        self._require_writer_entry(deadline)
        writer = self.writer_engine
        if self.dialect is Dialect.POSTGRESQL:
            return await self._snapshot_admission.connect_before(writer, deadline)

        async def connect() -> AsyncConnection:
            self._require_writer_entry(deadline)
            return await writer.connect()

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
        snapshot: bool = False,
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
                if verify_postgresql_writer and connection.in_transaction():
                    await complete(self._commit_verified_postgresql_writer(connection))
                elif transactional:
                    await self._run_snapshot_phase(deadline, connection.commit)
            except BaseException as error:
                if admission is not None and not admission.handed_off:
                    raise
                await self._rollback_unit(
                    connection,
                    error,
                    verify_snapshot=deadline is not None
                    or (snapshot and isinstance(error, asyncio.CancelledError)),
                )
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
                    preserve_cancellation=cancelled and (snapshot or before_handoff),
                )
        if deadline is not None:
            self._snapshot_admission.require_current(engine, deadline)

    async def _rollback_unit(
        self,
        connection: AsyncConnection,
        cause: BaseException,
        *,
        verify_snapshot: bool,
    ) -> None:
        if not verify_snapshot:
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
            if (
                not connection.closed
                and not connection.invalidated
                and connection.info.pop(NATIVE_READ_RETIRE, False)
            ):
                await complete(connection.invalidate())
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

    async def close_sqlite_serving(
        self, deadline_seconds: float = DEFAULT_CLOSE_DEADLINE_SECONDS
    ) -> None:
        lifetime = self._sqlite_serving
        if lifetime is None:
            raise DatabaseConfigurationError("the serving observer was not installed")
        path = self.settings.sqlite_path.absolute()
        with NativeRetirement.observe(path) as retirement:

            async def settle() -> None:
                NativeRetirement.drain(path, report=False)
                await lifetime.wait_settled()
                await retirement()

            try:
                await self.close(deadline_seconds)
            finally:
                await complete(settle())

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
            if self._writer is not None:
                await self._writer.dispose()
        finally:
            if self._reader is not self._writer:
                await self._reader.dispose()
        if self._sqlite_serving is not None:
            self._sqlite_serving.disposed.set()
