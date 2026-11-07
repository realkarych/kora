import asyncio
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest
from aiosqlite import Connection as SQLiteDriverConnection
from sqlalchemy import event, text
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.pool.base import ConnectionPoolEntry
from sqlalchemy.util import await_

from kora.db.connection import Database, Dialect
from tests.db.closing import CONDITION_DEADLINE_SECONDS, ConnectionLedger, until
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
