import asyncio
import sqlite3
import threading
from contextlib import AsyncExitStack, closing
from pathlib import Path

import pytest
from aiosqlite import Connection as SQLiteDriverConnection
from sqlalchemy import event, text
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.pool.base import ConnectionPoolEntry
from sqlalchemy.util import await_

from kora.db.connection import (
    SQLITE_READERS,
    Database,
    Dialect,
    SQLiteSynchronizationError,
)
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    ConnectionLedger,
    sqlite_companions,
    until,
)
from tests.support.databases import sqlite_settings


async def writer_settings(connection: AsyncConnection) -> tuple[object, object, object]:
    return (
        await connection.scalar(text("PRAGMA main.journal_mode")),
        await connection.scalar(text("PRAGMA main.synchronous")),
        await connection.scalar(text("PRAGMA foreign_keys")),
    )


@pytest.mark.asyncio
async def test_ordinary_database_writer_keeps_its_existing_mode(
    database: Database,
) -> None:
    if database.dialect is Dialect.SQLITE:
        async with database.transaction() as writer:
            settings = await writer_settings(writer)
        async with database.read_transaction() as reader:
            query_only = await reader.scalar(text("PRAGMA query_only"))

        assert settings == ("wal", 1, 1)
        assert query_only == 1
    else:
        async with database.transaction() as writer:
            result = await writer.scalar(text("SELECT 1"))

        with pytest.raises(ValueError, match="SQLite writer mode requires SQLite"):
            Database(database.settings, require_sqlite_full_sync=True)

        assert result == 1


@pytest.mark.asyncio
async def test_sqlite_full_sync_initializes_first_and_replacement_writer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "metadata.db"
    database = Database(sqlite_settings(path), require_sqlite_full_sync=True)
    ledger = ConnectionLedger(database)

    def start_writer_at_normal(
        raw_connection: DBAPIConnection, record: ConnectionPoolEntry
    ) -> None:
        with closing(raw_connection.cursor()) as cursor:
            cursor.execute("PRAGMA main.synchronous=NORMAL")

    event.listen(
        database.writer_engine.sync_engine,
        "connect",
        start_writer_at_normal,
        insert=True,
    )

    try:
        async with database.transaction() as writer:
            first = await writer_settings(writer)
        await database.writer_engine.dispose()
        with closing(sqlite3.connect(path, isolation_level=None)) as native:
            restored_rollback_journal = native.execute(
                "PRAGMA main.journal_mode=DELETE"
            ).fetchone()
        async with database.migration_connection() as writer:
            replacement = await writer_settings(writer)
    finally:
        await database.close()

    assert first == replacement == ("wal", 2, 1)
    assert restored_rollback_journal == ("delete",)
    assert ledger.opened == ledger.closed == ledger.checkouts == 2
    assert ledger.outstanding == 0
    assert ledger.closed_while_checked_out == 0
    assert database.writer_is_free()
    assert database.units_in_flight() == 0


@pytest.mark.parametrize("cancel", [False, True], ids=["refused", "cancelled"])
@pytest.mark.asyncio
async def test_sqlite_wal_refusal_releases_driver_and_writer(
    tmp_path: Path, cancel: bool
) -> None:
    path = tmp_path / "metadata.db"
    database = Database(sqlite_settings(path), require_sqlite_full_sync=True)
    entered = asyncio.Event()
    release = asyncio.Event()
    unit_ran = asyncio.Event()
    drivers: list[SQLiteDriverConnection] = []
    existing_threads = set(threading.enumerate())
    new_workers: set[threading.Thread] = set()

    def observe_first_connection(
        raw_connection: DBAPIConnection, record: ConnectionPoolEntry
    ) -> None:
        if drivers:
            return
        driver: object = record.driver_connection
        assert isinstance(driver, SQLiteDriverConnection)
        drivers.append(driver)
        new_workers.update(
            worker
            for worker in set(threading.enumerate()) - existing_threads
            if "_connection_worker_thread" in worker.name
        )
        entered.set()
        await_(release.wait())

    async def run_writer() -> None:
        async with database.transaction():
            unit_ran.set()

    event.listen(
        database.writer_engine.sync_engine,
        "connect",
        observe_first_connection,
        insert=True,
    )
    task: asyncio.Task[None] | None = None

    try:
        with closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as holder:
            holder.execute("CREATE TABLE probe (id INTEGER)")
            holder.execute("BEGIN")
            holder.execute("SELECT COUNT(*) FROM probe").fetchone()
            task = asyncio.create_task(run_writer())
            await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)
            if cancel:
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                with pytest.raises(OperationalError) as refusal:
                    await task
                original = refusal.value.orig
                assert isinstance(original, sqlite3.OperationalError)
                assert original.sqlite_errorcode == sqlite3.SQLITE_BUSY

            assert not unit_ran.is_set()
            assert database.writer_is_free()
            assert database.units_in_flight() == 0
            assert len(drivers) == len(new_workers) == 1
            with pytest.raises(ValueError, match="no active connection"):
                assert not drivers[0].in_transaction
            await until(lambda: all(not worker.is_alive() for worker in new_workers))
            holder.rollback()

        async with database.transaction() as writer:
            recovered = await writer_settings(writer)
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await database.close()

    assert recovered == ("wal", 2, 1)


async def reader_settings(
    connection: AsyncConnection,
) -> tuple[object, object, object, object]:
    return (
        *await writer_settings(connection),
        await connection.scalar(text("PRAGMA query_only")),
    )


async def all_reader_settings(
    database: Database,
) -> list[tuple[object, object, object, object]]:
    async with AsyncExitStack() as readers:
        connections = [
            await readers.enter_async_context(database.read_transaction())
            for _ in range(SQLITE_READERS)
        ]
        return [await reader_settings(connection) for connection in connections]


@pytest.mark.asyncio
async def test_ordinary_sqlite_reader_can_open_before_writer(tmp_path: Path) -> None:
    database = Database(sqlite_settings(tmp_path / "metadata.db"))

    try:
        async with database.read_transaction() as reader:
            journal_mode, _, foreign_keys, query_only = await reader_settings(reader)
    finally:
        await database.close()

    assert (journal_mode, foreign_keys, query_only) == ("delete", 1, 1)


@pytest.mark.asyncio
async def test_checkpoint_reader_reinitializes_and_can_truncate_wal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "metadata.db"
    wal = path.with_name(f"{path.name}-wal")
    database = Database(sqlite_settings(path), require_sqlite_full_sync=True)
    ledger = ConnectionLedger(database)

    def start_reader_at_normal(
        raw_connection: DBAPIConnection, record: ConnectionPoolEntry
    ) -> None:
        with closing(raw_connection.cursor()) as cursor:
            cursor.execute("PRAGMA main.synchronous=NORMAL")

    event.listen(
        database.reader_engine.sync_engine,
        "connect",
        start_reader_at_normal,
        insert=True,
    )

    try:
        async with database.transaction() as writer:
            await writer.execute(text("CREATE TABLE probe (id INTEGER)"))
            await writer.execute(text("INSERT INTO probe VALUES (7)"))
        initial_settings = await all_reader_settings(database)
        await database.reader_engine.dispose()
        async with database.read_transaction() as reader:
            replacement_settings = await reader_settings(reader)
            before_checkpoint = wal.stat().st_size
            result = (
                await reader.execute(text("PRAGMA main.wal_checkpoint(TRUNCATE)"))
            ).one()
            after_checkpoint = wal.stat().st_size
            query_only = await reader.scalar(text("PRAGMA query_only"))
            value = await reader.scalar(text("SELECT id FROM probe"))
    finally:
        await database.close()

    assert initial_settings == [("wal", 2, 1, 1)] * SQLITE_READERS
    assert replacement_settings == ("wal", 2, 1, 1)
    assert before_checkpoint > 0
    assert tuple(result) == (0, 0, 0)
    assert after_checkpoint == 0
    assert (query_only, value) == (1, 7)
    assert ledger.opened == ledger.closed == SQLITE_READERS + 2
    assert ledger.outstanding == 0
    assert ledger.closed_while_checked_out == 0
    assert sqlite_companions(database) == []


@pytest.mark.asyncio
async def test_pre_wal_reader_refusal_releases_driver_and_all_pool_slots(
    tmp_path: Path,
) -> None:
    database = Database(
        sqlite_settings(tmp_path / "metadata.db"), require_sqlite_full_sync=True
    )
    drivers: list[SQLiteDriverConnection] = []
    workers: list[threading.Thread] = []

    def observe_worker() -> int:
        if not workers:
            workers.append(threading.current_thread())
        return 0

    async def identify_worker(driver: SQLiteDriverConnection) -> None:
        await driver.set_progress_handler(observe_worker, 1)
        try:
            async with driver.execute("SELECT 1") as statement:
                await statement.fetchone()
        finally:
            await driver.set_progress_handler(observe_worker, 0)

    def observe_refused_driver(
        raw_connection: DBAPIConnection, record: ConnectionPoolEntry
    ) -> None:
        if not drivers:
            driver: object = record.driver_connection
            assert isinstance(driver, SQLiteDriverConnection)
            drivers.append(driver)
            await_(identify_worker(driver))

    event.listen(
        database.reader_engine.sync_engine,
        "connect",
        observe_refused_driver,
        insert=True,
    )
    body_ran = False

    try:
        with pytest.raises(SQLiteSynchronizationError, match="reader requires WAL"):
            async with database.read_transaction():
                body_ran = True
        assert len(drivers) == len(workers) == 1
        with pytest.raises(ValueError, match="no active connection"):
            assert not drivers[0].in_transaction
        await until(lambda: not workers[0].is_alive())
        idle_after_refusal = database.units_in_flight()
        ledger = ConnectionLedger(database)

        async with database.transaction() as writer:
            await writer.execute(text("CREATE TABLE probe (id INTEGER)"))
        recovered = await all_reader_settings(database)
    finally:
        await database.close()

    assert not body_ran
    assert idle_after_refusal == 0
    assert recovered == [("wal", 2, 1, 1)] * SQLITE_READERS
    assert ledger.opened == ledger.closed == ledger.checkouts == SQLITE_READERS + 1
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
    assert database.units_in_flight() == 0
    assert sqlite_companions(database) == []
