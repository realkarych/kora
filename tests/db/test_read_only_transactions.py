import asyncio
import json
from collections.abc import AsyncIterator
from sqlite3 import SQLITE_READONLY
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import Connection, Engine, event, update
from sqlalchemy.engine import AdaptedConnection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.conformance.capture import captured
from kora.db import DB_COMPONENT, TRANSACTION_FAILED
from kora.db.connection import (
    Database,
    DatabaseClosedError,
    DatabaseSettings,
    Dialect,
    Lifecycle,
)
from kora.db.tables import namespace
from kora.observability import Level, LoggingSettings
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    ConnectionLedger,
    narrow_settings,
    until,
)
from tests.db.seed import FakeClock
from tests.db.test_read_snapshot import (
    PostgresNativeConnection,
    assert_native_closed,
    physical_handle,
    revision,
    seed,
    settings,
)

PRIVATE_VALUE: str = "private-reader-write-canary"
READ_LOGGING: LoggingSettings = LoggingSettings(level=Level.DEBUG)
READ_SETUP: str = "SET TRANSACTION READ ONLY"


class ReaderEntry:
    def __init__(self, database: Database) -> None:
        self.database: Database = database
        self.engine: Engine = database.reader_engine.sync_engine
        self.entered: asyncio.Event = asyncio.Event()
        self.release: asyncio.Event = asyncio.Event()
        self.settled: asyncio.Event = asyncio.Event()
        self.handles: list[object] = []

    def install(self) -> None:
        if self.database.dialect is Dialect.SQLITE:
            event.listen(self.engine, "checkout", self._checkout)
        else:
            event.listen(self.engine, "after_cursor_execute", self._statement)

    def remove(self) -> None:
        if self.database.dialect is Dialect.SQLITE:
            event.remove(self.engine, "checkout", self._checkout)
        else:
            event.remove(self.engine, "after_cursor_execute", self._statement)

    def _checkout(self, connection: object, *arguments: object) -> None:
        assert isinstance(connection, AdaptedConnection)
        driver: object = connection.driver_connection
        self._pause(driver)

    def _statement(
        self, connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement == READ_SETUP:
            assert isinstance(connection, Connection)
            self._pause(connection.connection.driver_connection)

    def _pause(self, driver: object) -> None:
        if self.entered.is_set():
            return
        self.handles.append(physical_handle(self.database, driver))
        self.entered.set()
        await_(self.release.wait())
        self.settled.set()


@pytest_asyncio.fixture
async def ordinary_database(
    database_settings: DatabaseSettings,
) -> AsyncIterator[Database]:
    database = Database(narrow_settings(database_settings, pool_size=1))
    try:
        yield database
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("callback", [False, True], ids=["context", "callback"])
async def test_ordinary_read_refuses_persistent_metadata_writes(
    ordinary_database: Database, callback: bool
) -> None:
    database = ordinary_database
    ledger = ConnectionLedger(database)
    namespace_id = await seed(database)
    modes: list[tuple[object, ...]] = []

    async def accidental_write(connection: AsyncConnection) -> None:
        modes.append(await settings(connection, database.dialect))
        await connection.execute(
            update(namespace)
            .where(namespace.c.id == namespace_id)
            .values(name=PRIVATE_VALUE, policy_revision=2)
        )

    async def write_through_reader() -> None:
        if callback:
            await database.read(accidental_write)
        else:
            async with database.read_transaction() as connection:
                await accidental_write(connection)

    with captured(
        DB_COMPONENT, READ_LOGGING, "ordinary-read", FakeClock().now
    ) as output:
        with pytest.raises(DBAPIError) as refused:
            await write_through_reader()
    records = output.finish()
    async with database.read_transaction() as connection:
        retained_revision = await revision(connection, namespace_id)

    assert retained_revision == 1
    assert len(modes) == 1
    if database.dialect is Dialect.SQLITE:
        assert modes[0] == (1,)
        assert getattr(refused.value.orig, "sqlite_errorcode", None) == SQLITE_READONLY
    else:
        assert modes[0][1:] == ("read committed", "on")
        assert getattr(refused.value.orig, "sqlstate", None) == "25006"
    assert PRIVATE_VALUE in str(refused.value)
    assert any(record["event"] == TRANSACTION_FAILED.name for record in records)
    encoded = json.dumps(records)
    assert PRIVATE_VALUE not in encoded
    assert namespace_id not in encoded
    assert "UPDATE namespace" not in encoded
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_ordinary_read_mode_does_not_escape_to_writer_or_migration(
    ordinary_database: Database,
) -> None:
    database = ordinary_database
    ledger = ConnectionLedger(database)
    namespace_id = await seed(database)
    read_modes: list[tuple[object, ...]] = []

    async def failed_read(write: bool) -> None:
        async with database.read_transaction() as connection:
            read_modes.append(await settings(connection, database.dialect))
            if not write:
                raise RuntimeError("reader body failed")
            await connection.execute(
                update(namespace)
                .where(namespace.c.id == namespace_id)
                .values(name=PRIVATE_VALUE)
            )

    async with database.read_transaction() as connection:
        read_modes.append(await settings(connection, database.dialect))
    with pytest.raises(RuntimeError, match="reader body failed"):
        await failed_read(False)
    with pytest.raises(DBAPIError):
        await failed_read(True)
    async with database.transaction() as connection:
        writer_mode = await settings(connection, database.dialect)
        await connection.execute(
            update(namespace)
            .where(namespace.c.id == namespace_id)
            .values(policy_revision=2)
        )
    async with database.migration_connection() as connection:
        migration_mode = await settings(connection, database.dialect)
        await connection.execute(
            update(namespace)
            .where(namespace.c.id == namespace_id)
            .values(policy_revision=3)
        )
        await connection.commit()
    async with database.read_snapshot() as connection:
        snapshot_mode = await settings(connection, database.dialect)
    async with database.read_transaction() as connection:
        read_modes.append(await settings(connection, database.dialect))
        retained_revision = await revision(connection, namespace_id)

    assert retained_revision == 3
    assert all(mode == read_modes[0] for mode in read_modes)
    if database.dialect is Dialect.POSTGRESQL:
        pid = read_modes[0][0]
        assert read_modes[0] == (pid, "read committed", "on")
        assert writer_mode == migration_mode == (pid, "read committed", "off")
        assert snapshot_mode == (pid, "repeatable read", "on")
        assert ledger.opened == 1
    else:
        assert read_modes[0] == snapshot_mode == (1,)
        assert writer_mode == migration_mode == (0,)
    assert ledger.closed_while_checked_out == 0
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("callback", [False, True], ids=["context", "callback"])
@pytest.mark.parametrize("during_entry", [False, True], ids=["body", "entry"])
async def test_ordinary_read_cancellation_keeps_original_native_ownership(
    ordinary_database: Database, callback: bool, during_entry: bool
) -> None:
    database = ordinary_database
    ledger = ConnectionLedger(database)
    namespace_id = await seed(database)
    entry = ReaderEntry(database)
    body_entered = asyncio.Event()
    release_body = asyncio.Event()
    body_finished = asyncio.Event()

    async def unit(connection: AsyncConnection) -> None:
        assert await revision(connection, namespace_id) == 1
        body_entered.set()
        await release_body.wait()
        body_finished.set()

    async def read() -> None:
        if callback:
            await database.read(unit)
        else:
            async with database.read_transaction() as connection:
                await unit(connection)

    entry.install()
    with captured(DB_COMPONENT, READ_LOGGING, "read-cancel", FakeClock().now) as output:
        task = asyncio.create_task(read())
        try:
            await asyncio.wait_for(entry.entered.wait(), CONDITION_DEADLINE_SECONDS)
            if not during_entry:
                entry.release.set()
                await asyncio.wait_for(body_entered.wait(), CONDITION_DEADLINE_SECONDS)
            assert task.cancel()
            cancellation_turn = asyncio.Event()
            asyncio.get_running_loop().call_soon(cancellation_turn.set)
            await cancellation_turn.wait()
            if during_entry or callback:
                assert not task.done()
                assert ledger.outstanding == database.units_in_flight() == 1
                assert task.cancel()
            entry.release.set()
            if callback:
                await asyncio.wait_for(body_entered.wait(), CONDITION_DEADLINE_SECONDS)
            release_body.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
        finally:
            entry.release.set()
            release_body.set()
            entry.remove()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    records = output.finish()
    async with database.transaction() as connection:
        await connection.execute(
            update(namespace)
            .where(namespace.c.id == namespace_id)
            .values(policy_revision=2)
        )
    async with database.read_transaction() as connection:
        retained_revision = await revision(connection, namespace_id)

    assert body_entered.is_set() is (callback or not during_entry)
    assert body_finished.is_set() is callback
    assert entry.settled.is_set()
    assert retained_revision == 2
    assert not any(record["event"] == TRANSACTION_FAILED.name for record in records)
    assert ledger.closed_while_checked_out == 0
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True], ids=["graceful", "expired"])
async def test_ordinary_read_entry_respects_completed_close(
    ordinary_database: Database, expired: bool
) -> None:
    database = ordinary_database
    ledger = ConnectionLedger(database)
    namespace_id = await seed(database)
    entry = ReaderEntry(database)
    body_entered = asyncio.Event()

    async def read() -> None:
        async with database.read_transaction() as connection:
            body_entered.set()
            assert await revision(connection, namespace_id) == 1

    entry.install()
    task = asyncio.create_task(read())
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entry.entered.wait(), CONDITION_DEADLINE_SECONDS)
        closing = asyncio.create_task(
            database.close(deadline_seconds=0 if expired else 5)
        )
        if expired:
            await asyncio.wait_for(closing, CONDITION_DEADLINE_SECONDS)
            assert database.lifecycle is Lifecycle.CLOSED
        else:
            await until(lambda: database.lifecycle is Lifecycle.CLOSING)
            assert not closing.done()
        assert ledger.closed == 0
        assert ledger.outstanding == database.units_in_flight() == 1
        entry.release.set()
        if expired:
            with pytest.raises(
                DatabaseClosedError, match="closed before the unit finished"
            ):
                await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
        else:
            await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
        await asyncio.wait_for(closing, CONDITION_DEADLINE_SECONDS)
    finally:
        entry.release.set()
        entry.remove()
        await asyncio.gather(task, return_exceptions=True)
        if closing is not None:
            await closing

    assert body_entered.is_set() is not expired
    assert entry.settled.is_set()
    assert ledger.opened == ledger.closed
    assert ledger.closed_while_checked_out == 0
    assert ledger.outstanding == database.units_in_flight() == 0
    assert_native_closed(entry.handles[0])


@pytest.mark.asyncio
async def test_ordinary_read_setup_failure_releases_checkout(
    ordinary_database: Database,
) -> None:
    database = ordinary_database
    ledger = ConnectionLedger(database)
    engine = database.reader_engine.sync_engine
    body_entered = False
    terminated: list[PostgresNativeConnection] = []
    original = (
        database.settings.sqlite_path if database.dialect is Dialect.SQLITE else None
    )
    backup = original.with_suffix(".original") if original is not None else None

    def terminate_before_setup(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement == READ_SETUP:
            assert isinstance(connection, Connection)
            driver = cast(
                PostgresNativeConnection, connection.connection.driver_connection
            )
            terminated.append(driver)
            driver.terminate()

    if original is not None and backup is not None:
        original.rename(backup)
        original.mkdir()
    else:
        event.listen(engine, "before_cursor_execute", terminate_before_setup)

    try:
        with pytest.raises(DBAPIError):
            async with database.read_transaction():
                body_entered = True
        outstanding = ledger.outstanding
        units = database.units_in_flight()
    finally:
        if original is not None and backup is not None:
            original.rmdir()
            backup.rename(original)
        else:
            event.remove(engine, "before_cursor_execute", terminate_before_setup)
    namespace_id = await seed(database)
    async with database.read_transaction() as connection:
        retained_revision = await revision(connection, namespace_id)

    assert not body_entered
    assert outstanding == units == 0
    assert retained_revision == 1
    assert len(terminated) == (1 if database.dialect is Dialect.POSTGRESQL else 0)
    assert all(driver.is_closed() for driver in terminated)
    assert ledger.outstanding == database.units_in_flight() == 0
