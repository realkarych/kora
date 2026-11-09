import asyncio
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from traceback import walk_tb

import pytest
from aiosqlite import Connection as SQLiteDriverConnection
from aiosqlite import Cursor as SQLiteCursor
from sqlalchemy import Connection, event, text
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.pool import PoolResetState
from sqlalchemy.pool.base import ConnectionPoolEntry
from sqlalchemy.util import await_

from kora.db.connection import Database, SQLiteSynchronizationError
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    ConnectionLedger,
    sqlite_companions,
)
from tests.support.databases import sqlite_settings


def sqlite_driver(candidate: object) -> SQLiteDriverConnection:
    assert isinstance(candidate, SQLiteDriverConnection)
    return candidate


async def writer_driver(connection: AsyncConnection) -> SQLiteDriverConnection:
    raw = await connection.get_raw_connection()
    return sqlite_driver(raw.driver_connection)


@pytest.mark.asyncio
async def test_ordinary_writer_closes_retained_cursor_when_native_begin_is_busy(
    tmp_path: Path,
) -> None:
    database = Database(sqlite_settings(tmp_path / "metadata.db"))
    ledger = ConnectionLedger(database)
    body_ran = False
    try:
        async with database.transaction() as writer:
            await writer.execute(text("CREATE TABLE probe (id INTEGER)"))
        async with database.writer_engine.connect() as connection:
            driver = await writer_driver(connection)
            async with driver.execute("PRAGMA busy_timeout") as cursor:
                prior: object = await cursor.fetchone()
            assert prior == (5000,)
            async with driver.execute("PRAGMA busy_timeout=0"):
                pass
            async with driver.execute("PRAGMA busy_timeout") as cursor:
                assert await cursor.fetchone() == (0,)

        try:
            with closing(
                sqlite3.connect(
                    database.settings.sqlite_path, timeout=0, isolation_level=None
                )
            ) as blocker:
                with closing(blocker.execute("BEGIN IMMEDIATE")):
                    pass

                with pytest.raises(sqlite3.OperationalError) as refused:
                    async with database.transaction():
                        body_ran = True
                native = refused.value
                assert native.sqlite_errorcode == sqlite3.SQLITE_BUSY
                failed_cursor = next(
                    frame.f_locals.get("self")
                    for frame, _ in walk_tb(native.__traceback__)
                    if frame.f_code is SQLiteCursor.execute.__code__
                )
                assert isinstance(failed_cursor, SQLiteCursor)
                try:
                    await failed_cursor.fetchone()
                except sqlite3.ProgrammingError as error:
                    cursor_closed = str(error) == "Cannot operate on a closed cursor."
                else:
                    cursor_closed = False
                    await failed_cursor.close()

                assert blocker.in_transaction
                assert not driver.in_transaction
                assert ledger.outstanding == database.units_in_flight() == 0
                assert database.writer_is_free()
                blocker.rollback()
        finally:
            async with database.writer_engine.connect() as connection:
                assert await writer_driver(connection) is driver
                async with driver.execute("PRAGMA busy_timeout=5000"):
                    pass
                async with driver.execute("PRAGMA busy_timeout") as cursor:
                    assert await cursor.fetchone() == prior

        async with database.transaction() as recovered:
            assert await writer_driver(recovered) is driver
            await recovered.execute(text("INSERT INTO probe VALUES (7)"))
            saved = await recovered.scalar(text("SELECT id FROM probe"))
    finally:
        await database.close()

    assert cursor_closed
    assert not body_ran
    assert saved == 7
    assert ledger.opened == ledger.closed == 1
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
    assert sqlite_companions(database) == []


@pytest.mark.parametrize(
    ("mode_change", "mode_query", "value", "migration"),
    [
        ("PRAGMA main.synchronous=NORMAL", "PRAGMA main.synchronous", 1, False),
        ("PRAGMA main.journal_mode=DELETE", "PRAGMA main.journal_mode", "delete", True),
        (None, "PRAGMA main.synchronous", 2, False),
    ],
    ids=["weaker-sync", "non-wal-migration", "native-readback-error"],
)
@pytest.mark.asyncio
async def test_durable_writer_entry_refuses_native_drift_and_rolls_back(
    tmp_path: Path,
    mode_change: str | None,
    mode_query: str,
    value: str | int,
    migration: bool,
) -> None:
    database = Database(
        sqlite_settings(tmp_path / "metadata.db"), require_sqlite_full_sync=True
    )
    ledger = ConnectionLedger(database)
    observed: list[tuple[str, bool]] = []
    pre_reset_states: list[bool] = []
    body_ran = False
    failure_type = (
        sqlite3.DatabaseError if mode_change is None else SQLiteSynchronizationError
    )
    lease = database.migration_connection if migration else database.transaction

    def observe_mode_readback(
        action: int,
        pragma: str | None,
        argument: str | None,
        schema: str | None,
        origin: str | None,
    ) -> int:
        if (
            action == sqlite3.SQLITE_PRAGMA
            and pragma in ("journal_mode", "synchronous")
            and schema == "main"
            and argument is None
        ):
            observed.append((pragma, driver.in_transaction))
            if mode_change is None and pragma == "synchronous":
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def observe_reset(
        raw_connection: DBAPIConnection,
        record: ConnectionPoolEntry,
        reset_state: PoolResetState,
    ) -> None:
        if record.driver_connection is driver:
            pre_reset_states.append(driver.in_transaction)

    async def enter_refused_lease() -> None:
        nonlocal body_ran
        async with lease() as writer:
            body_ran = True
            await writer.execute(text("INSERT INTO probe VALUES (7)"))

    try:
        async with database.transaction() as writer:
            await writer.execute(text("CREATE TABLE probe (id INTEGER)"))
        async with database.writer_engine.connect() as connection:
            driver = await writer_driver(connection)
            if mode_change is not None:
                async with driver.execute(mode_change) as cursor:
                    await cursor.fetchone()
            await driver.set_authorizer(observe_mode_readback)
        event.listen(database.writer_engine.sync_engine, "reset", observe_reset)

        with pytest.raises(failure_type) as refused:
            await enter_refused_lease()
        observed_at_refusal = list(observed)
        resets_at_refusal = list(pre_reset_states)
        idle_after_refusal = (
            database.writer_is_free() and database.units_in_flight() == 0
        )
        outstanding_after_refusal = ledger.outstanding

        async with database.writer_engine.connect() as connection:
            reused = await writer_driver(connection)
            await reused.set_authorizer(None)
            async with reused.execute(mode_query) as cursor:
                unchanged_mode: object = await cursor.fetchone()
        if mode_change is not None:
            await database.writer_engine.dispose()
        async with database.transaction() as writer:
            recovered = await writer_driver(writer)
            rows = (await writer.execute(text("SELECT id FROM probe"))).all()
    finally:
        await database.close()

    assert not body_ran
    assert observed_at_refusal == (
        [("journal_mode", True)]
        if migration
        else [("journal_mode", True), ("synchronous", True)]
    )
    assert resets_at_refusal == [False]
    assert idle_after_refusal
    assert outstanding_after_refusal == 0
    assert reused is driver
    assert unchanged_mode == (value,)
    assert rows == []
    if mode_change is None:
        assert isinstance(refused.value, sqlite3.DatabaseError)
        assert refused.value.sqlite_errorcode == sqlite3.SQLITE_AUTH
        assert recovered is driver
    else:
        assert recovered is not driver
    assert ledger.opened == ledger.closed
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
    assert sqlite_companions(database) == []


@pytest.mark.asyncio
async def test_verified_native_transaction_keeps_wal_and_full(tmp_path: Path) -> None:
    database = Database(
        sqlite_settings(tmp_path / "metadata.db"), require_sqlite_full_sync=True
    )

    try:
        async with database.transaction() as writer:
            with pytest.raises(
                OperationalError, match="Safety level may not be changed"
            ):
                await writer.execute(text("PRAGMA main.synchronous=NORMAL"))
            with pytest.raises(OperationalError, match="out of wal mode"):
                await writer.execute(text("PRAGMA main.journal_mode=DELETE"))
            journal_mode = await writer.scalar(text("PRAGMA main.journal_mode"))
            synchronous = await writer.scalar(text("PRAGMA main.synchronous"))
            await writer.execute(text("CREATE TABLE probe (id INTEGER)"))
        async with database.transaction() as writer:
            tables = await writer.scalar(
                text("SELECT count(*) FROM sqlite_schema WHERE name = 'probe'")
            )
    finally:
        await database.close()

    assert (journal_mode, synchronous, tables) == ("wal", 2, 1)
    assert sqlite_companions(database) == []


@pytest.mark.asyncio
async def test_cancelled_durable_writer_entry_releases_verified_native_transaction(
    tmp_path: Path,
) -> None:
    database = Database(
        sqlite_settings(tmp_path / "metadata.db"), require_sqlite_full_sync=True
    )
    ledger = ConnectionLedger(database)
    native_begun = asyncio.Event()
    observer_exited = asyncio.Event()
    release = asyncio.Event()
    drivers: list[SQLiteDriverConnection] = []
    body_ran = False

    def pause_after_native_begin(connection: Connection) -> None:
        driver = sqlite_driver(connection.connection.driver_connection)
        assert driver.in_transaction
        assert connection.get_transaction() is None
        drivers.append(driver)
        native_begun.set()
        try:
            await_(release.wait())
        finally:
            observer_exited.set()

    async def acquire_writer() -> None:
        nonlocal body_ran
        async with database.transaction():
            body_ran = True

    event.listen(database.writer_engine.sync_engine, "begin", pause_after_native_begin)

    task = asyncio.create_task(acquire_writer())
    try:
        await asyncio.wait_for(native_begun.wait(), CONDITION_DEADLINE_SECONDS)
        cancellation_turn = asyncio.Event()
        task.cancel()
        asyncio.get_running_loop().call_soon(cancellation_turn.set)
        await asyncio.wait_for(cancellation_turn.wait(), CONDITION_DEADLINE_SECONDS)
        still_paused = not observer_exited.is_set()
        still_owned = drivers[0].in_transaction and not database.writer_is_free()
        outstanding_during_cancellation = ledger.outstanding
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        event.remove(
            database.writer_engine.sync_engine, "begin", pause_after_native_begin
        )
        released = not drivers[0].in_transaction and database.writer_is_free()
        outstanding = ledger.outstanding
        async with database.transaction() as writer:
            recovered = await writer_driver(writer)
            await writer.execute(text("CREATE TABLE probe (id INTEGER)"))
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await database.close()

    assert not body_ran
    assert still_paused
    assert still_owned
    assert outstanding_during_cancellation == 1
    assert released
    assert outstanding == 0
    assert drivers == [recovered]
    assert ledger.opened == ledger.closed == 1
    assert ledger.checkouts == 2
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
    assert database.units_in_flight() == 0
    assert sqlite_companions(database) == []


@pytest.mark.parametrize("drift", ["before-entry", "before-commit", "readback-error"])
@pytest.mark.asyncio
async def test_durable_fullfsync_guards_the_live_macos_writer(
    tmp_path: Path, drift: str
) -> None:
    database = Database(
        sqlite_settings(tmp_path / "metadata.db"), require_sqlite_full_sync=True
    )
    ledger = ConnectionLedger(database)
    observed: list[tuple[bool, bool]] = []
    pre_reset_states: list[bool] = []
    staged = False
    macos = sys.platform == "darwin"

    def observe_fullfsync(
        action: int,
        pragma: str | None,
        argument: str | None,
        schema: str | None,
        origin: str | None,
    ) -> int:
        if (
            action == sqlite3.SQLITE_PRAGMA
            and pragma == "fullfsync"
            and argument is None
        ):
            observed.append((staged, driver.in_transaction))
            if drift == "readback-error" and staged:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def observe_reset(
        raw_connection: DBAPIConnection,
        record: ConnectionPoolEntry,
        reset_state: PoolResetState,
    ) -> None:
        if record.driver_connection is driver:
            pre_reset_states.append(driver.in_transaction)

    async def write() -> None:
        nonlocal staged
        async with database.transaction() as writer:
            await writer.execute(text("INSERT INTO probe VALUES (7)"))
            staged = True
            if drift == "before-commit":
                await writer.execute(text("PRAGMA fullfsync=OFF"))
            if drift == "readback-error":
                await (await writer_driver(writer)).set_authorizer(observe_fullfsync)

    try:
        async with database.transaction() as writer:
            await writer.execute(text("CREATE TABLE probe (id INTEGER)"))
        async with database.writer_engine.connect() as connection:
            driver = await writer_driver(connection)
            if drift == "before-entry":
                async with driver.execute("PRAGMA fullfsync=OFF") as cursor:
                    await cursor.fetchone()
            await driver.set_authorizer(observe_fullfsync)
        event.listen(database.writer_engine.sync_engine, "reset", observe_reset)

        if macos and drift == "readback-error":
            with pytest.raises(sqlite3.DatabaseError) as refused:
                await write()
            assert refused.value.sqlite_errorcode == sqlite3.SQLITE_AUTH
        elif macos:
            with pytest.raises(SQLiteSynchronizationError, match="requires fullfsync"):
                await write()
        else:
            await write()
        observed_at_outcome = list(observed)
        resets_at_outcome = list(pre_reset_states)
        idle_after_outcome = (
            database.writer_is_free() and database.units_in_flight() == 0
        )
        outstanding_at_outcome = ledger.outstanding

        async with database.writer_engine.connect() as connection:
            same_writer = await writer_driver(connection)
            await same_writer.set_authorizer(None)
            async with same_writer.execute("PRAGMA fullfsync") as cursor:
                retained_request: object = await cursor.fetchone()
        await database.writer_engine.dispose()
        async with database.transaction() as writer:
            replacement = await writer_driver(writer)
            rows = (await writer.execute(text("SELECT id FROM probe"))).all()
            new_request = await writer.scalar(text("PRAGMA fullfsync"))
    finally:
        await database.close()

    if macos:
        expected_observations = [(False, True)]
        if drift != "before-entry":
            expected_observations.append((True, True))
        assert observed_at_outcome == expected_observations
        assert staged == (drift != "before-entry")
        assert rows == []
    else:
        assert observed_at_outcome == []
        assert staged
        assert rows == [(7,)]
    assert resets_at_outcome == [False]
    assert idle_after_outcome
    assert outstanding_at_outcome == 0
    assert same_writer is driver
    assert replacement is not driver
    assert retained_request == (int(macos and drift == "readback-error"),)
    assert new_request == int(macos)
    assert ledger.opened == ledger.closed == 2
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
    assert sqlite_companions(database) == []
