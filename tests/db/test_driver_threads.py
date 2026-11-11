import asyncio
import threading
from dataclasses import replace
from pathlib import Path

import aiosqlite
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, DatabaseSettings, Dialect

DRIVER_THREAD_SUFFIX = "(_connection_worker_thread)"
ABSENT_DATABASE = "kora_absent_database"
BUSY_TIMEOUT_MILLISECONDS = 5000
THREAD_EXIT_DEADLINE_SECONDS = 5.0


def driver_threads() -> set[threading.Thread]:
    return {
        thread
        for thread in threading.enumerate()
        if thread.name.endswith(DRIVER_THREAD_SUFFIX) and thread.is_alive()
    }


async def busy_timeout(connection: AsyncConnection) -> object:
    return await connection.scalar(text("PRAGMA busy_timeout"))


async def driver_isolation_level(connection: AsyncConnection) -> object:
    raw = await connection.get_raw_connection()
    driver = raw.driver_connection
    assert isinstance(driver, aiosqlite.Connection)
    return driver.isolation_level


async def select_one(connection: AsyncConnection) -> object:
    return await connection.scalar(text("SELECT 1"))


@pytest.mark.asyncio
async def test_an_open_connection_owns_at_most_one_daemon_driver_thread(
    database_settings: DatabaseSettings,
) -> None:
    before = driver_threads()
    started: set[threading.Thread] = set()
    database = Database(database_settings)
    try:
        assert await database.run(select_one) == 1
        started = driver_threads() - before
        expected = 1 if database.dialect is Dialect.SQLITE else 0
        assert len(started) == expected
        assert all(thread.daemon for thread in started)
        if database.dialect is Dialect.SQLITE:
            assert await database.run(busy_timeout) == BUSY_TIMEOUT_MILLISECONDS
            assert await database.run(driver_isolation_level) is None
    finally:
        await database.close()
    for thread in started:
        await asyncio.to_thread(thread.join, THREAD_EXIT_DEADLINE_SECONDS)
    assert not driver_threads() - before


@pytest.mark.asyncio
async def test_a_failed_connection_leaves_no_driver_thread(
    database_settings: DatabaseSettings, tmp_path: Path
) -> None:
    if database_settings.dialect is Dialect.POSTGRESQL:
        target = ABSENT_DATABASE
    else:
        target = str(tmp_path / "unavailable")
    settings = replace(
        database_settings, url=database_settings.url.set(database=target)
    )
    database = Database(settings)
    if settings.dialect is Dialect.SQLITE:
        settings.sqlite_path.unlink()
        settings.sqlite_path.mkdir()
    before = driver_threads()
    try:
        for engine in (database.reader_engine, database.writer_engine):
            with pytest.raises(DBAPIError):
                await engine.connect()
            assert not driver_threads() - before
        for unit in (database.read, database.run):
            with pytest.raises(DBAPIError):
                await unit(select_one)
    finally:
        await database.close()
    assert not driver_threads() - before
