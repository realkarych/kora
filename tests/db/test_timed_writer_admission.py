import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from traceback import format_exception

import pytest
import pytest_asyncio
from aiosqlite import Connection as SQLiteDriverConnection
from sqlalchemy import Connection, event, text
from sqlalchemy.engine.interfaces import DBAPIConnection, DBAPICursor, ExecutionContext
from sqlalchemy.pool.base import ConnectionPoolEntry, PoolProxiedConnection
from sqlalchemy.util import await_

from kora.conformance.capture import captured
from kora.db.connection import (
    POSTGRESQL_WAL_SETTINGS_QUERY,
    Database,
    DatabaseSettings,
    DatabaseWriterFailure,
    DatabaseWriterRejected,
    Dialect,
    Lifecycle,
)
from kora.db.transactions import writer_transaction
from kora.observability import Level, LoggingSettings
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    ConnectionLedger,
    narrow_settings,
    until,
)
from tests.db.test_postgres_writer_settings import (
    PostgresDriverConnection,
    postgres_driver,
)
from tests.db.test_read_snapshot import assert_native_closed, physical_handle
from tests.db.test_sqlite_writer_entry import sqlite_driver, writer_driver
from tests.support.databases import sqlite_settings

ENTRY_SECONDS: float = 0.5
CREATE_PROBE: str = "CREATE TABLE writer_entry_probe (id INTEGER)"
INSERT_PROBE: str = "INSERT INTO writer_entry_probe VALUES (17)"
READ_PROBE: str = "SELECT id FROM writer_entry_probe"


@pytest_asyncio.fixture
async def timed_database(
    database_settings: DatabaseSettings,
) -> AsyncIterator[Database]:
    settings = narrow_settings(database_settings, pool_size=1)
    database = Database(
        settings,
        require_sqlite_full_sync=settings.dialect is Dialect.SQLITE,
        require_postgresql_wal_settings=settings.dialect is Dialect.POSTGRESQL,
    )
    try:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(CREATE_PROBE)
        yield database
    finally:
        await database.close()


async def enter(database: Database, deadline: float) -> None:
    async with database.transaction(admission_deadline=deadline) as writer:
        await writer.exec_driver_sql(INSERT_PROBE)


@pytest.mark.asyncio
async def test_admitted_writer_keeps_caller_lease_and_commits_after_cutoff(
    timed_database: Database,
) -> None:
    database = timed_database
    ledger = ConnectionLedger(database)
    loop = asyncio.get_running_loop()
    expired = asyncio.Event()
    deadline = loop.time() + ENTRY_SECONDS
    handle = loop.call_at(deadline, expired.set)

    try:
        async with database.transaction(admission_deadline=deadline) as writer:
            lease = writer_transaction(writer)
            await asyncio.wait_for(expired.wait(), CONDITION_DEADLINE_SECONDS)
            assert writer_transaction(writer) is lease
            assert loop.time() >= deadline
            await writer.exec_driver_sql(INSERT_PROBE)
        async with database.read_transaction() as reader:
            rows = (await reader.exec_driver_sql(READ_PROBE)).scalars().all()
    finally:
        handle.cancel()

    assert rows == [17]
    assert database.units_in_flight() == ledger.outstanding == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_timed_writer_queue_releases_waiters_without_native_checkout(
    timed_database: Database,
) -> None:
    database = timed_database
    ledger = ConnectionLedger(database)
    loop = asyncio.get_running_loop()

    with pytest.raises(DatabaseWriterRejected) as already_expired:
        await enter(database, loop.time())
    async with database.transaction() as holder:
        await holder.exec_driver_sql(INSERT_PROBE)
        expired = asyncio.create_task(enter(database, loop.time() + ENTRY_SECONDS))
        cancelled = asyncio.create_task(
            enter(database, loop.time() + CONDITION_DEADLINE_SECONDS)
        )
        try:
            await until(lambda: database.units_in_flight() == 3)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            with pytest.raises(DatabaseWriterRejected) as refused:
                await expired
            assert ledger.checkouts == ledger.outstanding == 1
            assert database.units_in_flight() == 1
        finally:
            expired.cancel()
            cancelled.cancel()
            await asyncio.gather(expired, cancelled, return_exceptions=True)
    await enter(database, loop.time() + CONDITION_DEADLINE_SECONDS)
    async with database.read_transaction() as reader:
        count = await reader.scalar(text("SELECT count(*) FROM writer_entry_probe"))

    assert (
        already_expired.value.reason
        is refused.value.reason
        is DatabaseWriterFailure.DEADLINE
    )
    assert count == 2
    assert database.units_in_flight() == ledger.outstanding == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["checkout", "setup"])
@pytest.mark.parametrize("interruption", ["deadline", "cancel", "close"])
async def test_started_timed_writer_retains_native_phase_until_release(
    timed_database: Database, phase: str, interruption: str
) -> None:
    database = timed_database
    ledger = ConnectionLedger(database)
    loop = asyncio.get_running_loop()
    parked = asyncio.Event()
    release = asyncio.Event()
    engine = database.writer_engine.sync_engine
    native_active = False
    postgres: PostgresDriverConnection | None = None
    sqlite: SQLiteDriverConnection | None = None
    handle: object | None = None
    body_lent = False

    def park_checkout(
        raw: DBAPIConnection, record: ConnectionPoolEntry, proxy: PoolProxiedConnection
    ) -> None:
        nonlocal handle
        handle = physical_handle(database, record.driver_connection)
        parked.set()
        await_(release.wait())

    def park_sqlite_begin(connection: Connection) -> None:
        nonlocal native_active, sqlite, handle
        sqlite = sqlite_driver(connection.connection.driver_connection)
        handle = physical_handle(database, sqlite)
        native_active = sqlite.in_transaction
        parked.set()
        await_(release.wait())

    def park_postgres_wal(
        connection: Connection,
        cursor: DBAPICursor,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        nonlocal postgres, native_active, handle
        if statement == str(POSTGRESQL_WAL_SETTINGS_QUERY):
            postgres = postgres_driver(connection.connection.driver_connection)
            handle = physical_handle(database, postgres)
            native_active = postgres.is_in_transaction()
            parked.set()
            await_(release.wait())

    if phase == "checkout":
        event_name, listener = "checkout", park_checkout
    elif database.dialect is Dialect.SQLITE:
        event_name, listener = "begin", park_sqlite_begin
    else:
        event_name, listener = "after_cursor_execute", park_postgres_wal
    event.listen(engine, event_name, listener)

    deadline = loop.time() + (
        ENTRY_SECONDS if interruption == "deadline" else CONDITION_DEADLINE_SECONDS
    )

    async def attempt() -> None:
        nonlocal body_lent
        async with database.transaction(admission_deadline=deadline) as writer:
            body_lent = True
            await writer.exec_driver_sql(INSERT_PROBE)

    task = asyncio.create_task(attempt())
    try:
        await asyncio.wait_for(parked.wait(), CONDITION_DEADLINE_SECONDS)
        if interruption == "cancel":
            task.cancel()
            turn = asyncio.Event()
            loop.call_soon(turn.set)
            await asyncio.wait_for(turn.wait(), CONDITION_DEADLINE_SECONDS)
        elif interruption == "close":
            await database.close(deadline_seconds=0)
        else:
            await until(lambda: task.cancelling() > 0)
        still_owned = not task.done() and database.units_in_flight() == 1
        checked_out = ledger.outstanding
        assert postgres is None or postgres.is_in_transaction()
        assert sqlite is None or sqlite.in_transaction
        release.set()
        if interruption == "cancel":
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await task
            assert cancelled.value.__cause__ is cancelled.value.__context__ is None
        else:
            with pytest.raises(DatabaseWriterRejected) as refused:
                await task
            expected = (
                DatabaseWriterFailure.DEADLINE
                if interruption == "deadline"
                else DatabaseWriterFailure.UNAVAILABLE
            )
            assert refused.value.reason is expected
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, event_name, listener)

    if interruption != "close":
        assert sqlite is None or not sqlite.in_transaction
        assert postgres is None or not postgres.is_in_transaction()
        async with database.transaction() as writer:
            rows = (await writer.exec_driver_sql(READ_PROBE)).all()
        assert rows == []
    else:
        assert handle is not None
        assert_native_closed(handle)

    assert not body_lent
    assert still_owned
    assert checked_out == 1
    assert native_active is (phase == "setup")
    assert database.units_in_flight() == ledger.outstanding == 0
    assert database.writer_is_free()
    assert database.lifecycle is (
        Lifecycle.CLOSED if interruption == "close" else Lifecycle.OPEN
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["deadline", "cancel", "close"])
@pytest.mark.parametrize("durable", [False, True])
async def test_timed_sqlite_stops_before_native_begin_after_early_driver_phase(
    tmp_path: Path, interruption: str, durable: bool
) -> None:
    database = Database(
        sqlite_settings(tmp_path / "metadata.db"), require_sqlite_full_sync=durable
    )
    engine = database.writer_engine.sync_engine
    loop = asyncio.get_running_loop()
    parked = asyncio.Event()
    release = asyncio.Event()
    observed: list[tuple[str | None, str | None]] = []

    def observe(
        action: int,
        name: str | None,
        argument: str | None,
        schema: str | None,
        origin: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_TRANSACTION or (
            action == sqlite3.SQLITE_PRAGMA and name == "busy_timeout"
        ):
            observed.append((name, argument))
        return sqlite3.SQLITE_OK

    def park_before_hook(connection: Connection) -> None:
        with closing(connection.connection.cursor()):
            parked.set()
            await_(release.wait())

    def install_observer(connection: Connection) -> None:
        event.listen(connection, "begin", park_before_hook)

    async with database.transaction() as writer:
        await writer.exec_driver_sql(CREATE_PROBE)
        await writer.exec_driver_sql("PRAGMA busy_timeout=2301")
        original = await writer_driver(writer)
        await original.set_authorizer(observe)
    observed.clear()
    event.listen(engine, "engine_connect", install_observer)
    deadline = loop.time() + (
        ENTRY_SECONDS if interruption == "deadline" else CONDITION_DEADLINE_SECONDS
    )
    task = asyncio.create_task(enter(database, deadline))
    try:
        await asyncio.wait_for(parked.wait(), CONDITION_DEADLINE_SECONDS)
        if interruption == "close":
            await database.close(deadline_seconds=0)
        elif interruption == "cancel":
            task.cancel()
            turn = asyncio.Event()
            loop.call_soon(turn.set)
            await asyncio.wait_for(turn.wait(), CONDITION_DEADLINE_SECONDS)
        else:
            await until(lambda: task.cancelling() > 0)
        assert not task.done()
        release.set()
        if interruption == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(DatabaseWriterRejected) as refused:
                await task
            assert refused.value.reason is (
                DatabaseWriterFailure.DEADLINE
                if interruption == "deadline"
                else DatabaseWriterFailure.UNAVAILABLE
            )
        native_calls = list(observed)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "engine_connect", install_observer)
        await database.close()

    assert native_calls == [
        ("busy_timeout", None),
        ("busy_timeout", "0"),
        ("busy_timeout", None),
        ("busy_timeout", "2301"),
        ("busy_timeout", None),
    ]
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_timed_sqlite_writer_uses_zero_native_busy_mode_then_restores_prior_value(
    tmp_path: Path, durable: bool
) -> None:
    settings = sqlite_settings(tmp_path / "metadata.db")
    database = Database(settings, require_sqlite_full_sync=durable)
    native_calls: list[tuple[str | None, str | None]] = []
    deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS

    def observe(
        action: int,
        name: str | None,
        argument: str | None,
        schema: str | None,
        origin: str | None,
    ) -> int:
        if (action == sqlite3.SQLITE_PRAGMA and name == "busy_timeout") or (
            action == sqlite3.SQLITE_TRANSACTION and name == "BEGIN"
        ):
            native_calls.append((name, argument))
        return sqlite3.SQLITE_OK

    try:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(CREATE_PROBE)
            await writer.exec_driver_sql("PRAGMA busy_timeout=2301")
            driver = await writer_driver(writer)
            await driver.set_authorizer(observe)
        with closing(sqlite3.connect(settings.sqlite_path)) as competing:
            with closing(competing.execute("BEGIN IMMEDIATE")):
                with pytest.raises(DatabaseWriterRejected) as refused:
                    await enter(database, deadline)
                assert competing.in_transaction
                during_refusal = list(native_calls)
                competing.rollback()
        assert not driver.in_transaction
        async with database.transaction(admission_deadline=deadline) as writer:
            same_driver = await writer_driver(writer)
            restored = await writer.scalar(text("PRAGMA busy_timeout"))
            await writer.exec_driver_sql(INSERT_PROBE)
        async with database.read_transaction() as reader:
            rows = (await reader.exec_driver_sql(READ_PROBE)).scalars().all()
    finally:
        await database.close()

    assert refused.value.reason is DatabaseWriterFailure.UNAVAILABLE
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert during_refusal == [
        ("busy_timeout", None),
        ("busy_timeout", "0"),
        ("busy_timeout", None),
        ("BEGIN", None),
        ("busy_timeout", "2301"),
        ("busy_timeout", None),
    ]
    assert same_driver is driver
    assert restored == 2301
    assert rows == [17]
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_begin", [False, True])
async def test_unverified_sqlite_restoration_discards_original_native_handle(
    tmp_path: Path, fail_begin: bool
) -> None:
    database = Database(
        sqlite_settings(tmp_path / "metadata.db"), require_sqlite_full_sync=True
    )
    denied: list[tuple[str, bool]] = []
    deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS

    def deny(
        action: int,
        name: str | None,
        argument: str | None,
        schema: str | None,
        origin: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_PRAGMA and (
            (name == "busy_timeout" and argument == "2301")
            or (fail_begin and name == "synchronous")
        ):
            assert name is not None
            denied.append((name, driver.in_transaction))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    try:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(CREATE_PROBE)
            await writer.exec_driver_sql("PRAGMA busy_timeout=2301")
            driver = await writer_driver(writer)
            await driver.set_authorizer(deny)
        native: object = vars(driver)["_connection"]
        assert isinstance(native, sqlite3.Connection)

        with pytest.raises(DatabaseWriterRejected) as refused:
            await enter(database, deadline)
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            native.execute("SELECT 1")
        async with database.transaction(admission_deadline=deadline) as writer:
            replacement = await writer_driver(writer)
            rows = (await writer.exec_driver_sql(READ_PROBE)).all()
    finally:
        await database.close()

    assert denied == (
        [("synchronous", True), ("busy_timeout", False)]
        if fail_begin
        else [("busy_timeout", True)]
    )
    assert replacement is not driver
    assert refused.value.reason is DatabaseWriterFailure.UNAVAILABLE
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert rows == []
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["checkout", "begin"])
async def test_sqlite_cancel_survives_native_rollback_refusal(
    tmp_path: Path, phase: str
) -> None:
    database = Database(sqlite_settings(tmp_path / "metadata.db"))
    engine = database.writer_engine.sync_engine
    ledger = ConnectionLedger(database)
    loop = asyncio.get_running_loop()
    parked = asyncio.Event()
    release = asyncio.Event()
    denied: list[bool] = []
    deadline = loop.time() + CONDITION_DEADLINE_SECONDS

    def deny_rollback(
        action: int,
        name: str | None,
        argument: str | None,
        schema: str | None,
        origin: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_TRANSACTION and name == "ROLLBACK":
            denied.append(driver.in_transaction)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def park_begin(connection: Connection) -> None:
        parked.set()
        await_(release.wait())

    def park_checkout(connection: Connection) -> None:
        connection.begin()
        park_begin(connection)

    try:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(CREATE_PROBE)
            driver = await writer_driver(writer)
            await driver.set_authorizer(deny_rollback)
        native = physical_handle(database, driver)
        event_name, listener = (
            ("engine_connect", park_checkout)
            if phase == "checkout"
            else ("begin", park_begin)
        )
        event.listen(engine, event_name, listener)
        task = asyncio.create_task(enter(database, deadline))
        try:
            await asyncio.wait_for(parked.wait(), CONDITION_DEADLINE_SECONDS)
            assert driver.in_transaction
            task.cancel()
            turn = asyncio.Event()
            loop.call_soon(turn.set)
            await asyncio.wait_for(turn.wait(), CONDITION_DEADLINE_SECONDS)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            event.remove(engine, event_name, listener)
        assert_native_closed(native)
        await enter(database, deadline)
    finally:
        await database.close()

    assert denied
    assert all(denied)
    assert cancelled.value.__cause__ is cancelled.value.__context__ is None
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_timed_writer_cold_driver_failure_stays_private(
    database_settings: DatabaseSettings, tmp_path: Path
) -> None:
    private = "private-writer-entry-driver-source"
    sqlite = database_settings.dialect is Dialect.SQLITE
    target = str(tmp_path / private) if sqlite else private
    database = Database(
        replace(database_settings, url=database_settings.url.set(database=target))
    )
    if sqlite:
        database.settings.sqlite_path.unlink()
        database.settings.sqlite_path.mkdir()
    deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
    logging = LoggingSettings(
        level=Level.DEBUG,
        third_party={
            "sqlalchemy.pool": Level.DEBUG,
            "asyncpg": Level.DEBUG,
            "aiosqlite": Level.DEBUG,
        },
    )

    with captured("db", logging, "writer-test", lambda: datetime.now(UTC)) as capture:
        try:
            with pytest.raises(DatabaseWriterRejected) as refused:
                await enter(database, deadline)
        finally:
            await database.close()
        records = capture.finish()

    assert refused.value.reason is DatabaseWriterFailure.UNAVAILABLE
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert private not in "".join(format_exception(refused.value))
    assert private not in json.dumps(records)
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
