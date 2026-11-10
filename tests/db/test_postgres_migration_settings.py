import asyncio
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager, closing

import pytest
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.db.connection import (
    MIGRATION_LOCK_ID,
    POSTGRESQL_WAL_SETTINGS_QUERY,
    Database,
    DatabaseClosedError,
    DatabaseSettings,
    Dialect,
    PostgreSQLDurabilityError,
)
from kora.db.migrations import (
    current_revision,
    upgrade_database,
    upgrade_with_diagnostics,
)
from kora.db.migrations.environment import alembic_config, head_revision
from tests.db.cancel_cause_support import migration_lock_count
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    ConnectionLedger,
    narrow_settings,
)
from tests.db.test_postgres_writer_settings import (
    SHOW_COMMIT,
    postgres_driver,
    set_session_commit_mode,
)

CREATE_PROBE = "CREATE TABLE migration_wal_probe (id INTEGER)"
CREATE_INDEX = (
    "CREATE INDEX CONCURRENTLY migration_wal_probe_index ON migration_wal_probe (id)"
)
READ_STATE = text(
    "SELECT to_regclass('migration_wal_probe') IS NOT NULL, "
    "to_regclass('migration_wal_probe_index') IS NOT NULL, "
    "(SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory')"
)

MIGRATION_NATIVE_STATE = text(
    "SELECT pg_catalog.pg_backend_pid(), pg_catalog.current_setting('lock_timeout'), "
    "(SELECT count(*) FROM pg_catalog.pg_locks "
    "WHERE pid = pg_catalog.pg_backend_pid() AND locktype = 'advisory' "
    "AND classid = 0 AND objid = CAST(:key AS pg_catalog.oid) AND objsubid = 1 "
    "AND mode = 'ExclusiveLock' AND granted)"
)


def guarded_database(settings: DatabaseSettings) -> Database:
    postgres = settings.dialect is Dialect.POSTGRESQL
    return Database(
        narrow_settings(settings, pool_size=1),
        require_postgresql_wal_settings=postgres,
        require_sqlite_full_sync=not postgres,
    )


@asynccontextmanager
async def owned_database(
    settings: DatabaseSettings, *, guarded: bool = True
) -> AsyncGenerator[Database, None]:
    database = (
        guarded_database(settings)
        if guarded
        else Database(narrow_settings(settings, pool_size=1))
    )
    ledger = ConnectionLedger(database)
    try:
        yield database
    finally:
        await database.close()
    assert ledger.opened == ledger.closed
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
    assert database.units_in_flight() == 0


def observed_commit_mode(connection: Connection) -> tuple[object, ...]:
    with closing(connection.connection.cursor()) as cursor:
        cursor.execute(SHOW_COMMIT)
        observed: Iterable[object] | None = cursor.fetchone()
        assert observed is not None
        return tuple(observed)


async def transactional_create(
    database: Database, *, weak: bool, commit_modes: list[tuple[object, ...]]
) -> None:
    def observe_commit(connection: Connection) -> None:
        commit_modes.append(observed_commit_mode(connection))

    async with database.migration_connection() as writer:
        if database.dialect is Dialect.POSTGRESQL:
            mode = "off" if weak else "remote_apply"
            await writer.execute(
                text("SELECT pg_catalog.set_config('synchronous_commit', :mode, true)"),
                {"mode": mode},
            )
        await writer.exec_driver_sql(CREATE_PROBE)
        if database.dialect is Dialect.POSTGRESQL:
            event.listen(writer.sync_connection, "commit", observe_commit)
            try:
                await writer.commit()
            finally:
                event.remove(writer.sync_connection, "commit", observe_commit)


@pytest.mark.asyncio
async def test_migration_commit_refuses_weak_transactional_ddl_and_releases_lease(
    empty_database_settings: DatabaseSettings,
) -> None:
    modes: list[tuple[object, ...]] = []

    async with owned_database(empty_database_settings) as database:
        postgres = database.dialect is Dialect.POSTGRESQL
        if postgres:
            driver = await set_session_commit_mode(database, "on")
            with pytest.raises(
                PostgreSQLDurabilityError, match="requires durable WAL settings"
            ):
                await transactional_create(database, weak=True, commit_modes=modes)
            async with database.read_transaction() as reader:
                refused = tuple((await reader.execute(READ_STATE)).one())
                native = postgres_driver(
                    (await reader.get_raw_connection()).driver_connection
                )
            assert refused == (False, False, 0)
            assert modes == []
            assert native is driver
            assert not driver.is_in_transaction()
        await transactional_create(database, weak=False, commit_modes=modes)
        async with database.read_transaction() as reader:
            visible = await reader.scalar(
                text("SELECT COUNT(*) FROM migration_wal_probe")
            )

    assert visible == 0
    if postgres:
        assert modes == [("remote_apply",)]


def concurrent_index(
    connection: Connection, weak: bool, dispatched: list[tuple[object, ...]]
) -> None:
    migration = MigrationContext.configure(
        connection, opts={"transaction_per_migration": True}
    )

    def observe_dispatch(
        connection: Connection, cursor: object, statement: str, *args: object
    ) -> None:
        if statement == CREATE_INDEX:
            dispatched.append(observed_commit_mode(connection))

    event.listen(connection, "before_cursor_execute", observe_dispatch)
    try:
        with migration.begin_transaction(_per_migration=True):
            with migration.autocommit_block():
                if weak:
                    connection.exec_driver_sql("SET SESSION synchronous_commit = 'off'")
                connection.exec_driver_sql(CREATE_INDEX)
    finally:
        event.remove(connection, "before_cursor_execute", observe_dispatch)


async def create_index(
    database: Database, weak: bool, dispatched: list[tuple[object, ...]]
) -> None:
    async with database.migration_connection() as writer:
        await writer.run_sync(concurrent_index, weak, dispatched)


@pytest.mark.asyncio
async def test_migration_autocommit_checks_before_concurrent_index_dispatch(
    empty_database_settings: DatabaseSettings,
) -> None:
    dispatched: list[tuple[object, ...]] = []

    async with owned_database(empty_database_settings) as database:
        postgres = database.dialect is Dialect.POSTGRESQL
        await transactional_create(database, weak=False, commit_modes=[])
        if postgres:
            original = await set_session_commit_mode(database, "remote_apply")
            with pytest.raises(
                PostgreSQLDurabilityError, match="requires durable WAL settings"
            ):
                await create_index(database, True, dispatched)
            async with database.read_transaction() as reader:
                refused = tuple((await reader.execute(READ_STATE)).one())
                session = await reader.scalar(text(SHOW_COMMIT))
            assert refused == (True, False, 0)
            assert session == "off"
            assert dispatched == []
            recovered = await set_session_commit_mode(database, "remote_apply")
            assert recovered is original
            await create_index(database, False, dispatched)
            async with database.read_transaction() as reader:
                accepted = tuple((await reader.execute(READ_STATE)).one())
        else:
            async with database.migration_connection() as writer:
                await writer.exec_driver_sql(
                    "CREATE INDEX migration_wal_probe_index ON migration_wal_probe (id)"
                )
            async with database.read_transaction() as reader:
                accepted = await reader.scalar(
                    text(
                        "SELECT count(*) FROM sqlite_master "
                        "WHERE name='migration_wal_probe_index'"
                    )
                )

    assert accepted == ((True, True, 0) if postgres else 1)
    if postgres:
        assert dispatched == [("remote_apply",)]


@pytest.mark.asyncio
async def test_migration_empty_logical_commits_and_fake_commits_do_not_read_wal(
    empty_database_settings: DatabaseSettings,
) -> None:
    states: list[bool] = []
    async with owned_database(empty_database_settings) as database:
        if database.dialect is Dialect.SQLITE:
            async with database.migration_connection():
                assert database.units_in_flight() == 1
            return

        def native_state(connection: Connection) -> None:
            states.append(
                postgres_driver(
                    connection.connection.driver_connection
                ).is_in_transaction()
            )

        def change_mode_in_empty_block(connection: Connection) -> None:
            migration = MigrationContext.configure(
                connection, opts={"transaction_per_migration": True}
            )
            with migration.begin_transaction(_per_migration=True):
                with migration.autocommit_block():
                    connection.exec_driver_sql("SET SESSION synchronous_commit = 'off'")

        async with database.migration_connection() as writer:
            event.listen(writer.sync_connection, "commit", native_state)
            try:
                transaction = await writer.begin()
                await transaction.commit()
                await writer.run_sync(change_mode_in_empty_block)
            finally:
                event.remove(writer.sync_connection, "commit", native_state)
        await set_session_commit_mode(database, "on")

    assert states == [False, False, False, False]


async def current_and_head(connection: AsyncConnection) -> tuple[str | None, str]:
    def revisions(sync: Connection) -> tuple[str | None, str]:
        return current_revision(sync), head_revision(alembic_config(sync))

    return await connection.run_sync(revisions)


@pytest.mark.asyncio
async def test_opted_in_migration_upgrades_the_existing_history(
    empty_database_settings: DatabaseSettings,
) -> None:
    report = await upgrade_database(
        empty_database_settings, open_database=guarded_database
    )

    async with owned_database(empty_database_settings) as database:
        async with database.read_transaction() as connection:
            current, expected = await current_and_head(connection)

    assert report.from_revision is None
    assert report.steps >= 12
    assert report.to_revision == current == expected


@pytest.mark.asyncio
async def test_migration_dispatches_exact_native_lease_operations(
    empty_database_settings: DatabaseSettings,
) -> None:
    dispatches: list[tuple[str, object]] = []
    key = {"key": MIGRATION_LOCK_ID}

    def observe_dispatch(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        *arguments: object,
    ) -> None:
        if any(
            name in statement
            for name in ("set_config(", "pg_advisory_lock(", "pg_advisory_unlock(")
        ):
            dispatches.append((statement, parameters))

    async with owned_database(empty_database_settings, guarded=False) as database:
        if database.dialect is Dialect.SQLITE:
            async with database.migration_connection() as writer:
                report = await writer.run_sync(upgrade_with_diagnostics)
            assert report.from_revision is None
            assert report.steps > 0
            return

        async with database.transaction() as writer:
            await writer.exec_driver_sql("SET SESSION lock_timeout = '2s'")
            original = tuple((await writer.execute(MIGRATION_NATIVE_STATE, key)).one())
        engine = database.writer_engine.sync_engine
        event.listen(engine, "after_cursor_execute", observe_dispatch)

        try:
            async with database.migration_connection() as writer:
                acquired = tuple(
                    (await writer.execute(MIGRATION_NATIVE_STATE, key)).one()
                )
                report = await writer.run_sync(upgrade_with_diagnostics)
                upgraded = tuple(
                    (await writer.execute(MIGRATION_NATIVE_STATE, key)).one()
                )
            async with database.read_transaction() as reader:
                restored = tuple(
                    (await reader.execute(MIGRATION_NATIVE_STATE, key)).one()
                )
        finally:
            event.remove(engine, "after_cursor_execute", observe_dispatch)

    setting = (
        "SELECT pg_catalog.set_config('lock_timeout', "
        "CAST($1 AS pg_catalog.text), CAST($2 AS pg_catalog.bool))"
    )
    assert report.from_revision is None
    assert report.steps > 0
    assert original[1:] == ("2s", 0)
    assert acquired == upgraded == (original[0], "5s", 1)
    assert restored == original
    assert dispatches == [
        (setting, ("5s", False)),
        (
            "SELECT pg_catalog.pg_advisory_lock(CAST($1 AS pg_catalog.int8))",
            (MIGRATION_LOCK_ID,),
        ),
        (setting, ("5s", True)),
        (setting, ("5s", True)),
        (
            "SELECT pg_catalog.pg_advisory_unlock(CAST($1 AS pg_catalog.int8))",
            (MIGRATION_LOCK_ID,),
        ),
        (setting, ("2s", False)),
    ]


@pytest.mark.asyncio
async def test_default_migration_preserves_ordinary_settings(
    empty_database_settings: DatabaseSettings,
) -> None:
    settings = narrow_settings(empty_database_settings, pool_size=1)
    database = Database(settings)
    postgres = database.dialect is Dialect.POSTGRESQL
    try:
        if postgres:
            await set_session_commit_mode(database, "off")
        async with database.migration_connection() as writer:
            observed = await writer.scalar(
                text(SHOW_COMMIT if postgres else "PRAGMA main.synchronous")
            )
            await writer.exec_driver_sql(CREATE_PROBE)
            if postgres:
                await writer.commit()
        async with database.read_transaction() as reader:
            visible = await reader.scalar(
                text("SELECT COUNT(*) FROM migration_wal_probe")
            )
    finally:
        await database.close()

    assert observed == ("off" if postgres else 1)
    assert visible == 0


async def ddl_after_close(database: Database) -> None:
    def attempt(connection: Connection) -> None:
        migration = MigrationContext.configure(
            connection, opts={"transaction_per_migration": True}
        )
        with migration.begin_transaction(_per_migration=True):
            with migration.autocommit_block():
                await_(database.close(deadline_seconds=0))
                connection.exec_driver_sql(CREATE_INDEX)

    async with database.migration_connection() as writer:
        if database.dialect is Dialect.POSTGRESQL:
            await writer.run_sync(attempt)
        else:
            await writer.exec_driver_sql(
                "CREATE INDEX migration_wal_probe_index ON migration_wal_probe (id)"
            )
            await database.close(deadline_seconds=0)


@pytest.mark.asyncio
async def test_closed_migration_refuses_native_ddl_and_releases_resources(
    empty_database_settings: DatabaseSettings,
) -> None:
    async with owned_database(empty_database_settings) as database:
        await transactional_create(database, weak=False, commit_modes=[])

        with pytest.raises(
            DatabaseClosedError, match="closed before the unit finished"
        ):
            await ddl_after_close(database)

    async with owned_database(empty_database_settings) as reopened:
        async with reopened.read_transaction() as reader:
            if reopened.dialect is Dialect.POSTGRESQL:
                assert tuple((await reader.execute(READ_STATE)).one()) == (
                    True,
                    False,
                    0,
                )
            else:
                remaining = await reader.scalar(
                    text(
                        "SELECT count(*) FROM sqlite_master "
                        "WHERE name='migration_wal_probe_index'"
                    )
                )
                assert remaining == 0
        assert await migration_lock_count(reopened) == 0


@pytest.mark.asyncio
async def test_cancelled_migration_observation_rolls_back_and_releases_lease(
    empty_database_settings: DatabaseSettings,
) -> None:
    reading = asyncio.Event()
    never_ready = asyncio.Event()

    async def pause() -> None:
        worker = asyncio.current_task()
        assert worker is not None
        reading.set()
        try:
            await never_ready.wait()
        finally:
            asyncio.get_running_loop().call_soon(worker.cancel)

    def pause_settings(
        connection: Connection, cursor: object, statement: str, *args: object
    ) -> None:
        if statement == POSTGRESQL_WAL_SETTINGS_QUERY.text:
            await_(pause())

    async with owned_database(empty_database_settings) as database:
        postgres = database.dialect is Dialect.POSTGRESQL
        driver = await set_session_commit_mode(database, "on") if postgres else None

        async def interrupted_migration() -> None:
            async with database.migration_connection() as writer:
                await writer.exec_driver_sql(CREATE_PROBE)
                if not postgres:
                    await pause()
                    return
                event.listen(
                    writer.sync_connection, "before_cursor_execute", pause_settings
                )
                try:
                    await writer.commit()
                finally:
                    event.remove(
                        writer.sync_connection, "before_cursor_execute", pause_settings
                    )

        task = asyncio.create_task(interrupted_migration())
        try:
            await asyncio.wait_for(reading.wait(), CONDITION_DEADLINE_SECONDS)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
            assert task.cancelling() == 2
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)

        assert database.units_in_flight() == 0
        assert await migration_lock_count(database) == 0
        if driver is not None:
            assert not driver.is_in_transaction()
        async with database.read_transaction() as reader:
            if postgres:
                assert tuple((await reader.execute(READ_STATE)).one()) == (
                    False,
                    False,
                    0,
                )
                assert (
                    postgres_driver(
                        (await reader.get_raw_connection()).driver_connection
                    )
                    is driver
                )
            else:
                visible = await reader.scalar(
                    text(
                        "SELECT count(*) FROM sqlite_master "
                        "WHERE name='migration_wal_probe'"
                    )
                )
                assert visible == 0
