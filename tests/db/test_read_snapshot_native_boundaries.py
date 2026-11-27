import asyncio
import json
from datetime import UTC, datetime
from traceback import format_exception, walk_tb
from typing import cast

import aiosqlite
import pytest
from sqlalchemy import Connection, event, select, text
from sqlalchemy.util import await_

from kora.conformance.capture import captured
from kora.db.connection import (
    Database,
    DatabaseSettings,
    DatabaseSnapshotFailure,
    DatabaseSnapshotRejected,
    Dialect,
)
from kora.observability import Level, LoggingSettings
from kora.sqlite.driver import Connection as GuardedConnection
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    ConnectionLedger,
    WriterCutoffLoop,
)
from tests.db.native_read_support import (
    NativeQuery,
    PostgresReadConnection,
    owned_reader,
)
from tests.db.test_read_snapshot import PostgresNativeConnection, physical_handle


@pytest.mark.asyncio
async def test_snapshot_external_native_cancellation_is_unavailable(
    database_settings: DatabaseSettings, database: Database
) -> None:
    async with owned_reader(database_settings) as reader:
        probe = NativeQuery(reader)
        deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
        task = asyncio.create_task(probe.run(deadline))
        await probe.wait_started(database)

        if isinstance(probe.driver, aiosqlite.Connection):
            await probe.driver.interrupt()
        else:
            async with database.read_transaction() as connection:
                assert await connection.scalar(
                    text("SELECT pg_cancel_backend(:pid)"),
                    {
                        "pid": cast(
                            PostgresReadConnection, probe.driver
                        ).get_server_pid()
                    },
                )
        with pytest.raises(DatabaseSnapshotRejected) as refused:
            await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
        await probe.assert_reused()

        assert refused.value.reason is DatabaseSnapshotFailure.UNAVAILABLE
        assert refused.value.__cause__ is refused.value.__context__ is None


def test_snapshot_reuses_one_deadline_before_each_native_dispatch(
    database_settings: DatabaseSettings,
) -> None:
    loop = WriterCutoffLoop()
    dispatched: list[str] = []

    def observe(cursor: object, statement: str, *arguments: object) -> None:
        dispatched.append(statement)

    async def read_twice(database: Database, deadline: float) -> None:
        async with database.read_snapshot(deadline=deadline) as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
            loop.advance_to(deadline)
            await connection.exec_driver_sql("SELECT 2")

    async def exercise() -> None:
        async with owned_reader(database_settings) as database:
            event.listen(database.reader_engine.sync_engine, "do_execute", observe)
            deadline = loop.time() + CONDITION_DEADLINE_SECONDS
            with pytest.raises(DatabaseSnapshotRejected) as refused:
                await read_twice(database, deadline)
            event.remove(database.reader_engine.sync_engine, "do_execute", observe)

            assert refused.value.reason is DatabaseSnapshotFailure.DEADLINE
            assert database.units_in_flight() == 0

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        runner.run(exercise())

    assert "SELECT 1" in dispatched
    assert "SELECT 2" not in dispatched


def test_snapshot_does_not_relabel_native_failure_after_late_cleanup(
    database_settings: DatabaseSettings,
) -> None:
    loop = WriterCutoffLoop()

    async def exercise() -> None:
        async with owned_reader(database_settings) as database:
            deadline = loop.time() + CONDITION_DEADLINE_SECONDS

            def advance_during_rollback(connection: object) -> None:
                loop.advance_to(deadline)

            event.listen(
                database.reader_engine.sync_engine, "rollback", advance_during_rollback
            )

            with pytest.raises(DatabaseSnapshotRejected) as refused:
                async with database.read_snapshot(deadline=deadline) as connection:
                    await connection.exec_driver_sql("SELECT absent_native_column")
            event.remove(
                database.reader_engine.sync_engine, "rollback", advance_during_rollback
            )

            assert refused.value.reason is DatabaseSnapshotFailure.UNAVAILABLE
            assert database.units_in_flight() == 0

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        runner.run(exercise())


def close_native(database: Database, connection: Connection) -> None:
    driver = connection.connection.driver_connection
    native = physical_handle(database, driver)
    if isinstance(native, GuardedConnection):
        native.close()
    else:
        cast(PostgresNativeConnection, native).terminate()


async def fail_native_snapshot(database: Database, phase: str) -> None:
    async with database.read_snapshot(
        deadline=asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
    ) as connection:
        if phase == "query":
            await connection.exec_driver_sql("SELECT private_native_snapshot_source")
        else:
            assert await connection.scalar(select(1)) == 1
            native_connection = connection.sync_connection
            assert native_connection is not None
            close_native(database, native_connection)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["setup", "query", "restore"])
async def test_snapshot_guard_restores_settings_and_excludes_content(
    database_settings: DatabaseSettings, phase: str
) -> None:
    private = "private_native_snapshot_source"
    async with owned_reader(database_settings) as database:
        setup = (
            "BEGIN DEFERRED"
            if database.dialect is Dialect.SQLITE
            else "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        )

        def fail_setup(
            connection: Connection, cursor: object, statement: str, *arguments: object
        ) -> None:
            if phase == "setup" and statement == setup:
                close_native(database, connection)

        event.listen(
            database.reader_engine.sync_engine, "after_cursor_execute", fail_setup
        )
        with captured(
            "db",
            LoggingSettings(
                level=Level.DEBUG,
                third_party={
                    "sqlalchemy.pool": Level.DEBUG,
                    "asyncpg": Level.DEBUG,
                    "aiosqlite": Level.DEBUG,
                },
            ),
            "native-snapshot",
            lambda: datetime.now(UTC),
        ) as capture:
            try:
                with pytest.raises(DatabaseSnapshotRejected) as refused:
                    await fail_native_snapshot(database, phase)
            finally:
                event.remove(
                    database.reader_engine.sync_engine,
                    "after_cursor_execute",
                    fail_setup,
                )
            records = capture.finish()

        assert refused.value.reason is DatabaseSnapshotFailure.UNAVAILABLE
        assert refused.value.__cause__ is refused.value.__context__ is None
        assert private not in "".join(format_exception(refused.value))
        assert private not in json.dumps(records)
        frames = {
            frame.f_code.co_name: frame.f_locals
            for frame, _ in walk_tb(refused.value.__traceback__)
        }
        assert set(frames["read_snapshot"]) == {"reason"}
        assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", [False, True])
async def test_snapshot_guard_does_not_preserve_an_unrelated_cancelled_unit(
    database: Database, writer: bool
) -> None:
    entered = asyncio.Event()
    hold = asyncio.Event()
    ledger = ConnectionLedger(database)
    engine = database.writer_engine if writer else database.reader_engine

    def pause_statement(
        connection: Connection, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement == "SELECT 41":
            entered.set()
            await_(hold.wait())

    async def ordinary() -> None:
        context = database.transaction if writer else database.read_transaction
        async with context() as connection:
            await connection.exec_driver_sql("SELECT 41")

    event.listen(engine.sync_engine, "before_cursor_execute", pause_statement)
    try:
        async with database.read_snapshot(
            deadline=asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
        ) as guarded:
            assert await guarded.scalar(select(1)) == 1
            task = asyncio.create_task(ordinary())
            await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)

            task.cancel("ordinary cancellation")
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
            assert await guarded.scalar(select(1)) == 1
    finally:
        hold.set()
        event.remove(engine.sync_engine, "before_cursor_execute", pause_statement)

    assert ledger.closed == (0 if writer and database.dialect is Dialect.SQLITE else 1)
    assert ledger.outstanding == database.units_in_flight() == 0
