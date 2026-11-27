import asyncio
import math
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, field
from threading import Event
from time import monotonic
from typing import Protocol, cast, runtime_checkable

from aiosqlite import Connection as SQLiteDriver
from sqlalchemy import Connection, event
from sqlalchemy.engine.interfaces import DBAPICursor, ExceptionContext
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.pool.base import ConnectionPoolEntry, PoolProxiedConnection
from sqlalchemy.util import await_

from kora.db.graph_deployment_stage_reads import PAYLOAD_DIGEST_WIDTH, STORE_ID_WIDTH
from kora.db.graph_stage_promotion_custody_v1 import INTENT_TABLE

SQLITE_PROGRESS_INSTRUCTIONS: int = 1000
NATIVE_READ_GUARD: str = "kora_native_read_guard"
NATIVE_READ_RETIRE: str = "kora_native_read_retire"
MAX_TIMEOUT_MILLISECONDS: int = 2**31 - 1
POSTGRESQL_TIMEOUT: str = "SHOW statement_timeout"
SET_POSTGRESQL_TIMEOUT: str = (
    "SELECT pg_catalog.set_config('statement_timeout', $1, true)"
)
TIMEOUT_UNITS: tuple[tuple[str, int], ...] = (
    ("ms", 1),
    ("s", 1000),
    ("min", 60000),
    ("h", 3600000),
    ("d", 86400000),
)

_SQLITE_INTENT_METADATA: str = """
SELECT rowid, ?, typeof(store_id), typeof(payload_digest),
       typeof(operation_id), typeof(request_digest),
       typeof(fence_incarnation), typeof(content_generation)
  FROM main.graph_deployment_stage_promotion_intent
       INDEXED BY sqlite_autoindex_graph_deployment_stage_promotion_intent_1
 WHERE stage_id COLLATE BINARY = ?
"""
PROMOTION_INTENT_TEXT_WIDTHS: tuple[tuple[str, int, int], ...] = (
    ("store_id", 1, STORE_ID_WIDTH),
    ("payload_digest", PAYLOAD_DIGEST_WIDTH, PAYLOAD_DIGEST_WIDTH),
    ("operation_id", 1, STORE_ID_WIDTH),
    ("request_digest", PAYLOAD_DIGEST_WIDTH, PAYLOAD_DIGEST_WIDTH),
)
_SQLITE_INTENT_STORAGE: tuple[str, ...] = (
    "text",
    "text",
    "text",
    "text",
    "integer",
    "integer",
)


class PromotionIntentScalarsRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("graph stage promotion intent scalars rejected")


def require_promotion_intent_width(
    width: object, lower: int, upper: int, factor: int
) -> None:
    if (
        type(width) is not int
        or not lower * factor <= width <= upper * factor
        or width % factor
    ):
        raise PromotionIntentScalarsRejected()


class _SQLiteIntentWidthRow:
    __slots__ = ()

    def __init__(self, cursor: sqlite3.Cursor, row: tuple[object, ...]) -> None:
        if len(row) != 2 + len(_SQLITE_INTENT_STORAGE):
            raise PromotionIntentScalarsRejected()
        rowid, factor, *storage = row
        if (
            type(rowid) is not int
            or type(factor) is not int
            or factor not in (1, 2)
            or tuple(storage) != _SQLITE_INTENT_STORAGE
        ):
            raise PromotionIntentScalarsRejected()
        for name, lower, upper in PROMOTION_INTENT_TEXT_WIDTHS:
            with cursor.connection.blobopen(
                INTENT_TABLE, name, rowid, readonly=True, name="main"
            ) as value:
                require_promotion_intent_width(len(value), lower, upper, factor)


def sqlite_record_worker(
    checkout: ConnectionPoolEntry | PoolProxiedConnection,
) -> SQLiteDriver | None:
    driver: object = checkout.driver_connection
    return driver if isinstance(driver, SQLiteDriver) else None


async def sqlite_checkout_worker(connection: AsyncConnection) -> SQLiteDriver | None:
    if connection.closed or connection.invalidated:
        return None
    return sqlite_record_worker(await connection.get_raw_connection())


async def observe_sqlite_promotion_intent(
    connection: AsyncConnection,
    stage_id: str,
    width_factor: int,
    check_deadline: Callable[[], None],
) -> bool:
    check_deadline()
    driver = await sqlite_checkout_worker(connection)
    if driver is None or not driver.in_transaction:
        raise PromotionIntentScalarsRejected()
    async with driver.cursor() as cursor:
        cursor.row_factory = _SQLiteIntentWidthRow
        check_deadline()
        await cursor.execute(_SQLITE_INTENT_METADATA, (width_factor, stage_id))
        check_deadline()
        value = cast(object, await cursor.fetchone())
    if value is None:
        return False
    if type(value) is not _SQLiteIntentWidthRow:
        raise PromotionIntentScalarsRejected()
    return True


class PostgreSQLDurabilityError(RuntimeError):
    pass


@runtime_checkable
class _PostgreSQLTransactionState(Protocol):
    def is_in_transaction(self) -> bool: ...


def postgresql_transaction_active(connection: Connection) -> bool:
    if connection.closed or connection.invalidated:
        raise PostgreSQLDurabilityError("PostgreSQL migration backend is unavailable")
    driver: object = connection.connection.driver_connection
    if not isinstance(driver, _PostgreSQLTransactionState):
        raise PostgreSQLDurabilityError("PostgreSQL migration backend is unavailable")
    active = driver.is_in_transaction()
    if type(active) is not bool:
        raise PostgreSQLDurabilityError("PostgreSQL migration backend is unavailable")
    return active


class SqliteProgressControl(Protocol):
    async def set_progress_handler(
        self, handler: Callable[[], int | None] | None, n: int
    ) -> None: ...


class NativeReadUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("native snapshot protection is unavailable")


class NativeReadExpired(RuntimeError):
    def __init__(self) -> None:
        super().__init__("native snapshot deadline expired")


def handle_native_read_error(context: ExceptionContext) -> None:
    connection = context.connection
    if connection is None or connection.closed or connection.invalidated:
        return
    guard = connection.info.get(NATIVE_READ_GUARD)
    if isinstance(guard, NativeReadGuard):
        guard.handle_error(context)


@dataclass(slots=True)
class ReadCutoff:
    deadline: float
    cancelled: Event = field(default_factory=Event)
    expired: Event = field(default_factory=Event)

    def progress(self) -> int:
        if monotonic() >= self.deadline:
            self.expired.set()
        return int(self.cancelled.is_set() or self.expired.is_set())


def timeout_value(cursor: DBAPICursor, *, postgresql: bool = False) -> int:
    row: Iterable[object] | None = cursor.fetchone()
    values = tuple(row) if row is not None else ()
    if len(values) != 1:
        raise NativeReadUnavailable()
    value = values[0]
    if postgresql:
        if value == "0":
            return 0
        if type(value) is not str:
            raise NativeReadUnavailable()
        for suffix, multiplier in TIMEOUT_UNITS:
            digits = value.removesuffix(suffix)
            if digits != value and digits.isascii() and digits.isdecimal():
                value = int(digits) * multiplier
                break
    if type(value) is not int or not 0 <= value <= MAX_TIMEOUT_MILLISECONDS:
        raise NativeReadUnavailable()
    return value


class NativeReadGuard:
    def __init__(self, connection: AsyncConnection, deadline: float) -> None:
        self.connection: AsyncConnection = connection
        self.deadline: float = deadline
        now = monotonic()
        self.cutoff: ReadCutoff = ReadCutoff(
            now + deadline - asyncio.get_running_loop().time()
        )
        self.raw: PoolProxiedConnection | None = None
        self.sqlite: SQLiteDriver | None = None
        self.original_timeout: int | None = None
        self.listening: bool = False
        self.interrupt_failed: bool = False
        self.native_errors: tuple[type[Exception], ...] = (
            SQLAlchemyError,
            sqlite3.Error,
            ValueError,
            OSError,
        )

    def require_time(self) -> int:
        remaining = self.deadline - asyncio.get_running_loop().time()
        if remaining < 0.001:
            raise NativeReadExpired()
        return min(MAX_TIMEOUT_MILLISECONDS, math.floor(remaining * 1000))

    def _configure(self, connection: Connection) -> None:
        self.require_time()
        raw = connection.connection
        self.raw = raw
        self.native_errors = (
            *self.native_errors,
            connection.dialect.loaded_dbapi.Error,
        )
        if NATIVE_READ_GUARD in raw.info:
            raise NativeReadUnavailable()
        raw.info[NATIVE_READ_GUARD] = self
        if connection.dialect.name == "sqlite":
            native = raw.driver_connection
            if not isinstance(native, SQLiteDriver):
                raise NativeReadUnavailable()
            self.sqlite = native
        with closing(raw.cursor()) as cursor:
            cursor.execute(
                "PRAGMA busy_timeout" if self.sqlite is not None else POSTGRESQL_TIMEOUT
            )
            self.original_timeout = timeout_value(
                cursor, postgresql=self.sqlite is None
            )

    async def install(self) -> None:
        try:
            await self.connection.run_sync(self._configure)
            if self.sqlite is not None:
                await self.sqlite.set_progress_handler(
                    self.cutoff.progress, SQLITE_PROGRESS_INSTRUCTIONS
                )
            self.require_time()
            event.listen(
                self.connection.sync_connection,
                "before_cursor_execute",
                self._before_statement,
            )
            self.listening = True
        except self.native_errors:
            raise NativeReadUnavailable() from None

    def _before_statement(
        self, connection: Connection, cursor: DBAPICursor, *arguments: object
    ) -> None:
        remaining = self.require_time()
        raw = self.raw
        original = self.original_timeout
        if raw is None or not raw.is_valid or original is None:
            raise NativeReadUnavailable()
        milliseconds = (
            min(original, remaining)
            if self.sqlite is not None or original > 0
            else remaining
        )
        try:
            with closing(raw.cursor()) as control:
                if self.sqlite is not None:
                    control.execute(f"PRAGMA busy_timeout={milliseconds}")
                else:
                    control.execute(SET_POSTGRESQL_TIMEOUT, (str(milliseconds),))
                    control.fetchone()
        except self.native_errors:
            raise NativeReadUnavailable() from None
        self.require_time()

    def handle_error(self, context: ExceptionContext) -> None:
        if context.connection is self.connection.sync_connection and isinstance(
            context.original_exception, asyncio.CancelledError
        ):
            context.is_disconnect = False
            try:
                await_(self.interrupt())
            except self.native_errors:
                self.interrupt_failed = True

    def expired_error(self, error: BaseException) -> bool:
        native = error.orig if isinstance(error, DBAPIError) else error
        return (
            self.cutoff.expired.is_set()
            and isinstance(native, sqlite3.Error)
            and native.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
        )

    async def interrupt(self) -> None:
        self.cutoff.cancelled.set()
        if self.sqlite is not None:
            await self.sqlite.interrupt()

    def _remove_listeners(self) -> None:
        if self.listening:
            event.remove(
                self.connection.sync_connection,
                "before_cursor_execute",
                self._before_statement,
            )
            self.listening = False
        if self.raw is not None and self.raw.info.get(NATIVE_READ_GUARD) is self:
            self.raw.info.pop(NATIVE_READ_GUARD)

    async def close(self) -> None:
        self._remove_listeners()
        native = self.sqlite
        try:
            if native is not None:
                await cast(SqliteProgressControl, native).set_progress_handler(None, 0)
                if self.original_timeout is not None:
                    async with native.execute(
                        f"PRAGMA busy_timeout={self.original_timeout}"
                    ) as cursor:
                        row: Iterable[object] | None = await cursor.fetchone()
                        values = tuple(row) if row is not None else ()
                        if values != (self.original_timeout,):
                            raise NativeReadUnavailable()
            if self.interrupt_failed:
                raise NativeReadUnavailable()
        except self.native_errors:
            raise NativeReadUnavailable() from None

    async def retire(self) -> None:
        if self.raw is not None:
            self.raw.info[NATIVE_READ_RETIRE] = True
        if self.sqlite is not None:
            await self.sqlite.close()
