import asyncio
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import event, select
from sqlalchemy.util import await_

from kora.db.connection import (
    Database,
    DatabaseSettings,
    DatabaseSnapshotFailure,
    DatabaseSnapshotRejected,
    Dialect,
    Lifecycle,
)
from tests.db.closing import CONDITION_DEADLINE_SECONDS, sqlite_contender, until
from tests.db.native_read_support import (
    NativeQuery,
    owned_reader,
    timeout_setting,
)


@pytest.mark.asyncio
async def test_snapshot_interrupts_native_query_at_deadline(
    database_settings: DatabaseSettings, database: Database
) -> None:
    async with owned_reader(database_settings) as reader:
        probe = NativeQuery(reader)
        deadline = asyncio.get_running_loop().time() + 1.0

        task = asyncio.create_task(probe.run(deadline))
        await probe.wait_started(database)
        with pytest.raises(DatabaseSnapshotRejected) as refused:
            await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
        await probe.assert_reused()

        assert probe.started.is_set()
        if reader.dialect is Dialect.SQLITE:
            assert refused.value.reason is DatabaseSnapshotFailure.DEADLINE
        else:
            assert refused.value.reason in (
                DatabaseSnapshotFailure.DEADLINE,
                DatabaseSnapshotFailure.UNAVAILABLE,
            )
        assert refused.value.__cause__ is refused.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_snapshot_cancellation_settles_before_connection_reuse(
    database_settings: DatabaseSettings, database: Database, shutdown: bool
) -> None:
    async with owned_reader(database_settings) as reader:
        probe = NativeQuery(reader)
        rollback_started = asyncio.Event()
        finish_rollback = asyncio.Event()

        def pause_rollback(connection: object) -> None:
            rollback_started.set()
            await_(finish_rollback.wait())

        event.listen(reader.reader_engine.sync_engine, "rollback", pause_rollback)
        deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
        task = asyncio.create_task(probe.run(deadline))
        close: asyncio.Task[None] | None = None

        try:
            await probe.wait_started(database)
            task.cancel("initial native cancellation")
            await asyncio.wait_for(rollback_started.wait(), CONDITION_DEADLINE_SECONDS)
            assert probe.ledger.outstanding == reader.units_in_flight() == 1
            assert probe.ledger.closed == 0
            assert task.cancel("repeated cancellation")
            if shutdown:
                close = asyncio.create_task(reader.close())
                await until(lambda: reader.lifecycle is Lifecycle.CLOSING)
        finally:
            finish_rollback.set()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
            event.remove(reader.reader_engine.sync_engine, "rollback", pause_rollback)
        if close is not None:
            await close
        else:
            await probe.assert_reused()

        assert cancelled.value.args == ("initial native cancellation",)
        assert probe.ledger.outstanding == reader.units_in_flight() == 0
        if shutdown:
            assert reader.lifecycle is Lifecycle.CLOSED
            assert probe.ledger.closed == probe.ledger.opened


@pytest.mark.asyncio
async def test_snapshot_preserves_tighter_native_timeout(
    database_settings: DatabaseSettings, database: Database
) -> None:
    async with owned_reader(database_settings) as reader:
        if reader.dialect is Dialect.SQLITE:
            with sqlite_contender(
                database_settings.sqlite_path, timeout_ms=5000
            ) as writer:
                with closing(writer.execute("PRAGMA journal_mode=DELETE")) as mode:
                    assert mode.fetchone() == ("delete",)
                async with reader.read_transaction() as connection:
                    await connection.exec_driver_sql("PRAGMA busy_timeout=20")
                with closing(writer.execute("BEGIN EXCLUSIVE")):
                    pass
                try:
                    with pytest.raises(DatabaseSnapshotRejected) as refused:
                        async with reader.read_snapshot(
                            deadline=asyncio.get_running_loop().time() + 5.0
                        ) as connection:
                            await connection.exec_driver_sql(
                                "SELECT name FROM namespace"
                            )
                finally:
                    writer.rollback()
            expected: object = 20
        else:
            async with reader.read_transaction() as connection:
                await connection.exec_driver_sql("SET statement_timeout = '20ms'")
            probe = NativeQuery(reader)
            with pytest.raises(DatabaseSnapshotRejected) as refused:
                await probe.run(asyncio.get_running_loop().time() + 5.0)
            expected = "20ms"
        async with reader.read_transaction() as connection:
            restored = await timeout_setting(connection, reader.dialect)
            assert await connection.scalar(select(1)) == 1

        assert refused.value.reason is DatabaseSnapshotFailure.UNAVAILABLE
        assert restored == expected
        assert reader.units_in_flight() == 0


@pytest.mark.asyncio
async def test_snapshot_native_busy_wait_uses_remaining_deadline(
    database_settings: DatabaseSettings,
) -> None:
    if database_settings.dialect is Dialect.SQLITE:
        with closing(sqlite3.connect(database_settings.sqlite_path)) as setup:
            setup.execute("PRAGMA journal_mode=DELETE")
    async with owned_reader(database_settings) as reader:
        observed: int | None = None
        async with reader.read_transaction() as connection:
            assert await connection.scalar(select(1)) == 1

        async def read_locked_table() -> None:
            nonlocal observed
            async with reader.read_snapshot(
                deadline=asyncio.get_running_loop().time() + 1.0
            ) as connection:
                query = (
                    "PRAGMA busy_timeout"
                    if reader.dialect is Dialect.SQLITE
                    else "SELECT setting::integer FROM pg_settings "
                    "WHERE name = 'statement_timeout'"
                )
                value = (await connection.exec_driver_sql(query)).scalar_one()
                assert type(value) is int
                observed = value
                await connection.exec_driver_sql("SELECT name FROM namespace")

        if reader.dialect is Dialect.POSTGRESQL:
            observer = Database(database_settings)
            try:
                async with observer.transaction() as writer:
                    await writer.exec_driver_sql(
                        "LOCK TABLE namespace IN ACCESS EXCLUSIVE MODE"
                    )
                    with pytest.raises(DatabaseSnapshotRejected):
                        await read_locked_table()
            finally:
                await observer.close()
            expected: object = "0"
        else:
            with sqlite_contender(
                database_settings.sqlite_path, timeout_ms=5000
            ) as writer:
                with closing(writer.execute("BEGIN EXCLUSIVE")):
                    pass
                try:
                    with pytest.raises(DatabaseSnapshotRejected):
                        await read_locked_table()
                finally:
                    writer.rollback()
            expected = 5000
        async with reader.read_transaction() as connection:
            restored = await timeout_setting(connection, reader.dialect)
            succeeded = await connection.scalar(select(1))

        assert observed is not None
        assert 0 < observed <= 1000
        assert restored == expected
        assert succeeded == 1
        assert reader.units_in_flight() == 0
