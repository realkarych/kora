import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Protocol, cast

import aiosqlite
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, DatabaseSettings, Dialect
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    ConnectionLedger,
    narrow_settings,
)

SQLITE_LONG_QUERY: str = (
    "WITH RECURSIVE numbers(value) AS (SELECT 1 UNION ALL "
    "SELECT value + 1 FROM numbers WHERE value < 1000000000) "
    "SELECT sum(value) FROM numbers"
)
POSTGRESQL_LONG_QUERY: str = "SELECT pg_sleep(3600)"
SQLITE_REUSE_QUERY: str = SQLITE_LONG_QUERY.replace("1000000000", "10000")


class PostgresReadConnection(Protocol):
    def get_server_pid(self) -> int: ...


class SqliteTraceControl(Protocol):
    async def set_trace_callback(
        self, handler: Callable[[str], None] | None
    ) -> None: ...


@asynccontextmanager
async def owned_reader(settings: DatabaseSettings) -> AsyncGenerator[Database]:
    database = Database(narrow_settings(settings, pool_size=1))
    try:
        yield database
    finally:
        await database.close()


class NativeQuery:
    def __init__(
        self, database: Database, observer: Callable[[str], None] | None = None
    ) -> None:
        self.database: Database = database
        self.driver: object = None
        self.started: asyncio.Event = asyncio.Event()
        self.ready: asyncio.Event = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        self.ledger: ConnectionLedger = ConnectionLedger(database)
        self.observer: Callable[[str], None] | None = observer

    def observe_sqlite(self, statement: str) -> None:
        if statement == SQLITE_LONG_QUERY:
            self.loop.call_soon_threadsafe(self.started.set)
        if self.observer is not None:
            self.observer(statement)

    async def run(self, deadline: float | None) -> None:
        async with self.database.read_snapshot(deadline=deadline) as connection:
            driver = (await connection.get_raw_connection()).driver_connection
            assert driver is not None
            self.driver = driver
            if isinstance(driver, aiosqlite.Connection):
                await cast(SqliteTraceControl, driver).set_trace_callback(
                    self.observe_sqlite
                )
                statement = SQLITE_LONG_QUERY
            else:
                statement = POSTGRESQL_LONG_QUERY
            self.ready.set()
            with await connection.exec_driver_sql(statement) as result:
                result.scalar_one()

    async def wait_started(self, observer: Database) -> None:
        async with asyncio.timeout(CONDITION_DEADLINE_SECONDS):
            await self.ready.wait()
            driver = self.driver
            assert driver is not None
            if isinstance(driver, aiosqlite.Connection):
                await self.started.wait()
            else:
                async with observer.read_transaction() as connection:
                    while not await connection.scalar(
                        text(
                            "SELECT state = 'active' AND query = :query "
                            "AND wait_event = 'PgSleep' "
                            "FROM pg_stat_activity WHERE pid = :pid"
                        ),
                        {
                            "pid": cast(
                                PostgresReadConnection, driver
                            ).get_server_pid(),
                            "query": POSTGRESQL_LONG_QUERY,
                        },
                    ):
                        await connection.exec_driver_sql(
                            "SELECT pg_stat_clear_snapshot()"
                        )
                self.started.set()

    async def assert_reused(self) -> None:
        assert self.ledger.outstanding == self.database.units_in_flight() == 0
        async with self.database.read_transaction() as connection:
            driver = (await connection.get_raw_connection()).driver_connection
            assert driver is self.driver
            if isinstance(driver, aiosqlite.Connection):
                await cast(SqliteTraceControl, driver).set_trace_callback(None)
                value = await connection.scalar(text(SQLITE_REUSE_QUERY))
                assert value == 50005000
                assert await timeout_setting(connection, Dialect.SQLITE) == 5000
            else:
                assert await connection.scalar(text("SELECT 1")) == 1
                assert await timeout_setting(connection, Dialect.POSTGRESQL) == "0"
        assert self.ledger.outstanding == self.database.units_in_flight() == 0
        assert self.ledger.closed == 0


async def timeout_setting(connection: AsyncConnection, dialect: Dialect) -> object:
    query = (
        "PRAGMA busy_timeout" if dialect is Dialect.SQLITE else "SHOW statement_timeout"
    )
    return await connection.scalar(text(query))
