import asyncio
import os
from collections.abc import Awaitable
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, runtime_checkable

import pytest
from sqlalchemy import Connection, event, text
from sqlalchemy.util import await_

from kora.db.connection import (
    POSTGRESQL_WAL_SETTINGS_QUERY,
    Database,
    DatabaseClosedError,
    DatabaseSettings,
    DatabaseSnapshotFailure,
    DatabaseSnapshotRejected,
    Dialect,
    PostgreSQLDurabilityError,
)
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    REFUSAL_DEADLINE_SECONDS,
    ConnectionLedger,
    WriterCutoffLoop,
    narrow_settings,
    until,
)
from tests.db.test_read_snapshot import assert_native_closed, physical_handle
from tests.db.test_sqlite_writer_entry import sqlite_driver
from tests.support.databases import (
    RYUK_VARIABLE,
    TEMPLATE_DATABASE,
    DatabaseKind,
    DisabledPostgresWALSetting,
    sqlite_settings,
    start_postgres,
)

CREATE_PROBE: str = "CREATE TABLE probe (id INTEGER)"
INSERT_PROBE: str = "INSERT INTO probe VALUES (7)"
READ_PROBE: str = "SELECT id FROM probe"
SHOW_COMMIT: str = "SHOW synchronous_commit"


@runtime_checkable
class PostgresDriverConnection(Protocol):
    def get_server_pid(self) -> int: ...
    def is_in_transaction(self) -> bool: ...


def postgres_driver(candidate: object) -> PostgresDriverConnection:
    assert isinstance(candidate, PostgresDriverConnection)
    return candidate


async def set_session_commit_mode(
    database: Database, mode: str
) -> PostgresDriverConnection:
    async with database.writer_engine.connect() as connection:
        raw = await connection.get_raw_connection()
        await connection.execute(
            text("SELECT pg_catalog.set_config('synchronous_commit', :mode, false)"),
            {"mode": mode},
        )
        await connection.commit()
        return postgres_driver(raw.driver_connection)


@pytest.mark.asyncio
async def test_ordinary_writer_remains_available_with_ordinary_settings(
    empty_database_settings: DatabaseSettings,
) -> None:
    settings = narrow_settings(empty_database_settings, pool_size=1)
    database = Database(settings)
    postgres = settings.dialect is Dialect.POSTGRESQL

    try:
        if postgres:
            await set_session_commit_mode(database, "off")
        async with database.transaction() as writer:
            mode: object = await writer.scalar(
                text(SHOW_COMMIT if postgres else "PRAGMA main.synchronous")
            )
            await writer.execute(text(CREATE_PROBE))
            await writer.execute(text(INSERT_PROBE))
        async with database.read_transaction() as reader:
            ids = (await reader.execute(text(READ_PROBE))).scalars().all()
    finally:
        await database.close()

    assert mode == ("off" if postgres else 1)
    assert ids == [7]


@pytest.mark.asyncio
async def test_postgres_entry_preserves_all_local_flush_modes_on_the_exact_backend(
    empty_database_settings: DatabaseSettings,
) -> None:
    settings = narrow_settings(empty_database_settings, pool_size=1)
    if settings.dialect is Dialect.SQLITE:
        with pytest.raises(ValueError, match="requires PostgreSQL"):
            Database(settings, require_postgresql_wal_settings=True)

        assert not settings.sqlite_path.exists()
        return

    database = Database(settings, require_postgresql_wal_settings=True)
    ledger = ConnectionLedger(database)
    observed: list[tuple[int, bool]] = []

    def observe_guard(
        connection: Connection, cursor: object, statement: str, *args: object
    ) -> None:
        if statement == POSTGRESQL_WAL_SETTINGS_QUERY.text:
            driver = postgres_driver(connection.connection.driver_connection)
            observed.append((driver.get_server_pid(), driver.is_in_transaction()))

    event.listen(
        database.writer_engine.sync_engine, "after_cursor_execute", observe_guard
    )
    modes = ("local", "remote_write", "on", "remote_apply")
    expected_backends: list[tuple[int, bool]] = []
    preserved: list[object] = []
    observations_at_entry: list[int] = []

    try:
        for mode in modes:
            driver = await set_session_commit_mode(database, mode)
            expected_backends.append((driver.get_server_pid(), True))
            async with database.transaction() as writer:
                observations_at_entry.append(len(observed))
                preserved.append(await writer.scalar(text(SHOW_COMMIT)))
    finally:
        await database.close()

    assert observations_at_entry == [1, 3, 5, 7]
    assert observed == [backend for backend in expected_backends for _ in range(2)]
    assert preserved == list(modes)
    assert ledger.opened == ledger.closed == 1
    assert ledger.outstanding == ledger.closed_while_checked_out == 0


async def refuse_weak_postgres_session(
    database: Database, ledger: ConnectionLedger
) -> None:
    body_ran = False

    async def enter_writer() -> None:
        nonlocal body_ran
        async with database.transaction() as writer:
            body_ran = True
            await writer.execute(text(INSERT_PROBE))

    driver = await set_session_commit_mode(database, "off")

    with pytest.raises(
        PostgreSQLDurabilityError, match="requires durable WAL settings"
    ):
        await enter_writer()
    idle_after_refusal = not driver.is_in_transaction()
    outstanding_after_refusal = ledger.outstanding
    async with database.read_transaction() as reader:
        retained_mode: object = await reader.scalar(text(SHOW_COMMIT))
        rows_before_recovery = (await reader.execute(text(READ_PROBE))).all()
    migration_lent = False
    with pytest.raises(
        PostgreSQLDurabilityError, match="requires durable WAL settings"
    ):
        async with database.migration_connection():
            migration_lent = True
    migration_idle = not driver.is_in_transaction()
    outstanding_after_migration = ledger.outstanding
    recovered = await set_session_commit_mode(database, "on")

    assert idle_after_refusal
    assert outstanding_after_refusal == 0
    assert retained_mode == "off"
    assert not migration_lent
    assert migration_idle
    assert outstanding_after_migration == 0
    assert rows_before_recovery == []
    assert recovered is driver
    assert not body_ran


@pytest.mark.asyncio
async def test_postgres_entry_refuses_weak_session_then_reuses_the_backend(
    empty_database_settings: DatabaseSettings,
) -> None:
    settings = narrow_settings(empty_database_settings, pool_size=1)
    postgres = settings.dialect is Dialect.POSTGRESQL
    database = Database(
        settings,
        require_postgresql_wal_settings=postgres,
        require_sqlite_full_sync=not postgres,
    )
    ledger = ConnectionLedger(database)

    try:
        async with database.transaction() as writer:
            await writer.execute(text(CREATE_PROBE))
        if postgres:
            await refuse_weak_postgres_session(database, ledger)
        async with database.transaction() as writer:
            await writer.execute(text(INSERT_PROBE))
        async with database.read_transaction() as reader:
            ids = (await reader.execute(text(READ_PROBE))).scalars().all()
    finally:
        await database.close()

    assert ids == [7]
    assert ledger.opened == ledger.closed == (1 if postgres else 2)
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
    assert database.units_in_flight() == 0


SnapshotProbe = tuple[object, tuple[object, ...], list[object]]


async def read_probe_snapshot(database: Database, deadline: float) -> SnapshotProbe:
    async with database.read_snapshot(deadline=deadline) as reader:
        raw = await reader.get_raw_connection()
        if database.dialect is Dialect.POSTGRESQL:
            result = await reader.exec_driver_sql(
                "SELECT current_setting('transaction_isolation'), "
                "current_setting('transaction_read_only'), "
                "current_setting('synchronous_commit')"
            )
        else:
            result = await reader.exec_driver_sql("PRAGMA query_only")
        mode: tuple[object, ...] = tuple(result.one())
        rows: list[object] = list((await reader.execute(text(READ_PROBE))).scalars())
        return raw.driver_connection, mode, rows


@pytest.mark.asyncio
async def test_timed_snapshot_composes_with_native_durable_writer_entry(
    empty_database_settings: DatabaseSettings,
) -> None:
    settings = narrow_settings(empty_database_settings, pool_size=1)
    postgres = settings.dialect is Dialect.POSTGRESQL
    database = Database(
        settings,
        require_postgresql_wal_settings=postgres,
        require_sqlite_full_sync=not postgres,
    )
    ledger = ConnectionLedger(database)
    engine = database.writer_engine.sync_engine
    entered = asyncio.Event()
    release = asyncio.Event()
    drivers: list[object] = []
    writes: list[int] = []

    def hold(connection: Connection) -> None:
        drivers.append(connection.connection.driver_connection)
        entered.set()
        await_(release.wait())

    def hold_postgres(
        connection: Connection, cursor: object, statement: str, *args: object
    ) -> None:
        if statement == POSTGRESQL_WAL_SETTINGS_QUERY.text:
            hold(connection)

    async def write() -> None:
        async with database.transaction() as writer:
            writes.append(7)
            await writer.execute(text(INSERT_PROBE))

    observer = "after_cursor_execute" if postgres else "begin"
    callback = hold_postgres if postgres else hold
    expired: asyncio.Task[SnapshotProbe] | None = None
    successor: asyncio.Task[SnapshotProbe] | None = None
    writer_task: asyncio.Task[None] | None = None
    observation: SnapshotProbe | None = None

    try:
        async with database.transaction() as writer:
            await writer.execute(text(CREATE_PROBE))
        if postgres:
            await set_session_commit_mode(database, "off")
        event.listen(engine, observer, callback)
        base_checkouts = ledger.checkouts
        writer_task = asyncio.create_task(write())

        try:
            await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)
            driver = drivers[0]
            loop = asyncio.get_running_loop()
            if postgres:
                expired = asyncio.create_task(
                    read_probe_snapshot(
                        database, loop.time() + REFUSAL_DEADLINE_SECONDS
                    )
                )
                await until(lambda: database.units_in_flight() == 2)
                done, _ = await asyncio.wait(
                    {expired}, timeout=CONDITION_DEADLINE_SECONDS
                )
                assert done == {expired}
                with pytest.raises(DatabaseSnapshotRejected) as refused:
                    await expired
                assert refused.value.reason is DatabaseSnapshotFailure.DEADLINE
                assert ledger.checkouts == base_checkouts + 1
                successor = asyncio.create_task(
                    read_probe_snapshot(
                        database, loop.time() + CONDITION_DEADLINE_SECONDS
                    )
                )
                await until(lambda: database.units_in_flight() == 2)
                assert not successor.done()
                assert postgres_driver(driver).is_in_transaction()
            else:
                observation = await read_probe_snapshot(
                    database, loop.time() + CONDITION_DEADLINE_SECONDS
                )
                assert observation[0] is not driver
                assert sqlite_driver(driver).in_transaction
            assert ledger.outstanding == 1
            assert writes == []
        finally:
            release.set()
        if successor is not None:
            with pytest.raises(PostgreSQLDurabilityError):
                await asyncio.wait_for(writer_task, CONDITION_DEADLINE_SECONDS)
            observation = await asyncio.wait_for(successor, CONDITION_DEADLINE_SECONDS)
            assert observation[0] is driver
            assert not postgres_driver(driver).is_in_transaction()
        else:
            await asyncio.wait_for(writer_task, CONDITION_DEADLINE_SECONDS)
    finally:
        tasks: list[Awaitable[object]] = [
            task for task in (writer_task, expired, successor) if task is not None
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        if event.contains(engine, observer, callback):
            event.remove(engine, observer, callback)
        await database.close()

    assert observation is not None
    _, mode, rows = observation
    assert mode == (("repeatable read", "on", "off") if postgres else (1,))
    assert rows == []
    assert writes == ([] if postgres else [7])
    assert ledger.checkouts == base_checkouts + 2
    assert ledger.opened == ledger.closed == (1 if postgres else 2)
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.parametrize("interrupt", ["cancel", "close"])
@pytest.mark.asyncio
async def test_durable_writer_owns_native_transaction_through_interrupted_entry(
    empty_database_settings: DatabaseSettings, interrupt: str
) -> None:
    settings = narrow_settings(empty_database_settings, pool_size=1)
    postgres = settings.dialect is Dialect.POSTGRESQL
    database = Database(
        settings,
        require_postgresql_wal_settings=postgres,
        require_sqlite_full_sync=not postgres,
    )
    ledger = ConnectionLedger(database)
    entry_started = asyncio.Event()
    release = asyncio.Event()
    drivers: list[PostgresDriverConnection] = []
    body_ran = False

    def pause_at_guard(
        connection: Connection, cursor: object, statement: str, *args: object
    ) -> None:
        if statement != POSTGRESQL_WAL_SETTINGS_QUERY.text:
            return
        driver = postgres_driver(connection.connection.driver_connection)
        assert driver.is_in_transaction()
        drivers.append(driver)
        entry_started.set()
        await_(release.wait())

    def pause_at_sqlite_checkout(*args: object) -> None:
        entry_started.set()
        await_(release.wait())

    async def enter_writer() -> None:
        nonlocal body_ran
        async with database.transaction() as writer:
            body_ran = True
            await writer.execute(text(CREATE_PROBE))

    engine = database.writer_engine.sync_engine
    if postgres:
        event.listen(engine, "after_cursor_execute", pause_at_guard)
    else:
        event.listen(engine, "checkout", pause_at_sqlite_checkout)

    task = asyncio.create_task(enter_writer())
    try:
        await asyncio.wait_for(entry_started.wait(), CONDITION_DEADLINE_SECONDS)
        if interrupt == "cancel":
            task.cancel()
            cancellation_turn = asyncio.Event()
            asyncio.get_running_loop().call_soon(cancellation_turn.set)
            await asyncio.wait_for(cancellation_turn.wait(), CONDITION_DEADLINE_SECONDS)
        else:
            await database.close(deadline_seconds=0)
        outstanding_during_interruption = ledger.outstanding
        held_during_interruption = database.units_in_flight()
        native_still_active = not postgres or drivers[0].is_in_transaction()
        task_still_parked = not task.done()
        release.set()
        refusal = (
            asyncio.CancelledError if interrupt == "cancel" else DatabaseClosedError
        )
        with pytest.raises(refusal):
            await task
        native_released = not postgres or not drivers[0].is_in_transaction()
        if interrupt == "cancel":
            if postgres:
                event.remove(engine, "after_cursor_execute", pause_at_guard)
            else:
                event.remove(engine, "checkout", pause_at_sqlite_checkout)
            async with database.transaction() as writer:
                await writer.execute(text(CREATE_PROBE))
                if postgres:
                    raw = await writer.get_raw_connection()
                    assert raw.driver_connection is drivers[0]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await database.close()

    assert not body_ran
    assert task_still_parked
    assert outstanding_during_interruption == held_during_interruption == 1
    assert native_still_active
    assert native_released
    assert ledger.opened == ledger.closed == 1
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
    assert database.units_in_flight() == 0


async def check_disabled_server_setting(
    settings: DatabaseSettings, disabled_setting: DisabledPostgresWALSetting
) -> None:
    ordinary = Database(settings)
    guarded = Database(settings, require_postgresql_wal_settings=True)
    ledger = ConnectionLedger(guarded)

    try:
        async with ordinary.transaction() as writer:
            observed: object = await writer.scalar(
                text("SELECT pg_catalog.current_setting(:setting)"),
                {"setting": str(disabled_setting)},
            )
            await writer.execute(text(CREATE_PROBE))
        with pytest.raises(
            PostgreSQLDurabilityError, match="requires durable WAL settings"
        ):
            async with guarded.transaction() as writer:
                await writer.execute(text(INSERT_PROBE))
        async with guarded.read_transaction() as reader:
            ids = (await reader.execute(text(READ_PROBE))).all()
    finally:
        try:
            await guarded.close()
        finally:
            await ordinary.close()

    assert observed == "off"
    assert ids == []
    assert ledger.opened == ledger.closed == 1
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
    assert guarded.units_in_flight() == 0


async def check_native_sqlite_settings(settings: DatabaseSettings) -> None:
    database = Database(settings, require_sqlite_full_sync=True)

    try:
        async with database.transaction() as writer:
            journal: object = await writer.scalar(text("PRAGMA main.journal_mode"))
            sync: object = await writer.scalar(text("PRAGMA main.synchronous"))
    finally:
        await database.close()

    assert (journal, sync) == ("wal", 2)


def test_writer_entry_checks_native_durability_settings(
    database_kind: DatabaseKind, tmp_path: Path
) -> None:
    if database_kind is DatabaseKind.SQLITE:
        asyncio.run(
            check_native_sqlite_settings(sqlite_settings(tmp_path / "metadata.db"))
        )
        return
    for disabled_setting in DisabledPostgresWALSetting:
        owned_server = contextmanager(start_postgres)
        with owned_server(
            os.environ.get(RYUK_VARIABLE), disabled_wal_setting=disabled_setting
        ) as server:
            asyncio.run(
                check_disabled_server_setting(
                    server.settings_for(TEMPLATE_DATABASE), disabled_setting
                )
            )


async def check_transaction_local_mode_before_commit(
    empty_database_settings: DatabaseSettings,
    local_mode: str,
    timed_admission: bool,
    cutoff_loop: WriterCutoffLoop,
) -> None:
    settings = narrow_settings(empty_database_settings, pool_size=1)
    postgres = settings.dialect is Dialect.POSTGRESQL
    database = Database(
        settings,
        require_postgresql_wal_settings=postgres,
        require_sqlite_full_sync=not postgres,
    )
    ledger = ConnectionLedger(database)
    observations: list[tuple[PostgresDriverConnection, int, bool]] = []
    commit_modes: list[object] = []
    engine = database.writer_engine.sync_engine

    def observe_guard(
        connection: Connection, cursor: object, statement: str, *args: object
    ) -> None:
        if statement == POSTGRESQL_WAL_SETTINGS_QUERY.text:
            driver = postgres_driver(connection.connection.driver_connection)
            observations.append(
                (driver, driver.get_server_pid(), driver.is_in_transaction())
            )

    def observe_commit(connection: Connection) -> None:
        if postgres:
            driver = postgres_driver(connection.connection.driver_connection)
            assert driver is original_driver
            assert driver.is_in_transaction()
            commit_modes.append(connection.exec_driver_sql(SHOW_COMMIT).scalar_one())
        else:
            commit_modes.append(
                connection.exec_driver_sql("PRAGMA synchronous").scalar_one()
            )

    async def stage_write() -> None:
        loop = asyncio.get_running_loop()
        cutoff = loop.time() + CONDITION_DEADLINE_SECONDS if timed_admission else None
        elapsed = asyncio.Event()
        barrier = loop.call_at(cutoff, elapsed.set) if cutoff is not None else None
        try:
            async with database.transaction(admission_deadline=cutoff) as writer:
                if postgres:
                    assert len(observations) == 1
                    await writer.execute(
                        text(
                            "SELECT pg_catalog.set_config("
                            "'synchronous_commit', :mode, true)"
                        ),
                        {"mode": local_mode},
                    )
                await writer.execute(text(INSERT_PROBE))
                if cutoff is not None:
                    cutoff_loop.advance_to(cutoff)
                    await asyncio.wait_for(elapsed.wait(), CONDITION_DEADLINE_SECONDS)
                    assert loop.time() >= cutoff
                    assert len(observations) == int(postgres)
        finally:
            if barrier is not None:
                barrier.cancel()

    refused = postgres and local_mode == "off"
    original_driver: PostgresDriverConnection | None = None
    restored_mode: object = None

    try:
        async with database.transaction() as writer:
            await writer.execute(text(CREATE_PROBE))
        if postgres:
            original_driver = await set_session_commit_mode(database, "on")
        event.listen(engine, "after_cursor_execute", observe_guard)
        event.listen(engine, "commit", observe_commit)
        checkouts_before_write = ledger.checkouts

        if refused:
            with pytest.raises(
                PostgreSQLDurabilityError, match="requires durable WAL settings"
            ):
                await stage_write()
        else:
            await stage_write()
        event.remove(engine, "commit", observe_commit)
        idle_after_write = (
            original_driver is None or not original_driver.is_in_transaction()
        )
        outstanding_after_write = ledger.outstanding
        assert ledger.checkouts == checkouts_before_write + 1
        async with database.read_transaction() as reader:
            if original_driver is not None:
                raw = await reader.get_raw_connection()
                assert raw.driver_connection is original_driver
                restored_mode = await reader.scalar(text(SHOW_COMMIT))
            ids = (await reader.execute(text(READ_PROBE))).scalars().all()
    finally:
        await database.close()

    if original_driver is not None:
        assert (
            observations
            == [(original_driver, original_driver.get_server_pid(), True)] * 2
        )
        assert restored_mode == "on"
    else:
        assert observations == []
    assert commit_modes == ([] if refused else [local_mode if postgres else 2])
    assert ids == ([] if refused else [7])
    assert idle_after_write
    assert outstanding_after_write == 0
    assert ledger.opened == ledger.closed == (1 if postgres else 2)
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
    assert database.units_in_flight() == 0


@pytest.mark.parametrize("local_mode", ["off", "remote_apply"])
@pytest.mark.parametrize("timed_admission", [False, True])
def test_durable_writer_checks_transaction_local_mode_before_commit(
    empty_database_settings: DatabaseSettings, local_mode: str, timed_admission: bool
) -> None:
    loop = WriterCutoffLoop()

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        runner.run(
            check_transaction_local_mode_before_commit(
                empty_database_settings, local_mode, timed_admission, loop
            )
        )


@pytest.mark.parametrize("interrupt", ["cancel", "close"])
@pytest.mark.parametrize("timed_admission", [False, True])
@pytest.mark.asyncio
async def test_durable_writer_owns_native_transaction_through_interrupted_commit(
    empty_database_settings: DatabaseSettings, interrupt: str, timed_admission: bool
) -> None:
    settings = narrow_settings(empty_database_settings, pool_size=1)
    postgres = settings.dialect is Dialect.POSTGRESQL
    database = Database(
        settings,
        require_postgresql_wal_settings=postgres,
        require_sqlite_full_sync=not postgres,
    )
    ledger = ConnectionLedger(database)
    commit_started = asyncio.Event()
    release = asyncio.Event()
    guard_drivers: list[PostgresDriverConnection] = []
    sqlite_handles: list[object] = []
    commit_dispatches = 0
    engine = database.writer_engine.sync_engine

    def pause_at_final_guard(
        connection: Connection, cursor: object, statement: str, *args: object
    ) -> None:
        if statement == POSTGRESQL_WAL_SETTINGS_QUERY.text:
            driver = postgres_driver(connection.connection.driver_connection)
            assert driver.is_in_transaction()
            guard_drivers.append(driver)
            if len(guard_drivers) == 2:
                commit_started.set()
                await_(release.wait())

    def observe_commit_dispatch(connection: Connection) -> None:
        nonlocal commit_dispatches
        commit_dispatches += 1
        if not postgres:
            sqlite_handles.append(
                physical_handle(database, connection.connection.driver_connection)
            )
            commit_started.set()
            await_(release.wait())

    async def stage_write() -> None:
        cutoff = (
            asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
            if timed_admission
            else None
        )
        async with database.transaction(admission_deadline=cutoff) as writer:
            await writer.execute(text(INSERT_PROBE))

    task: asyncio.Task[None] | None = None
    snapshot: asyncio.Task[SnapshotProbe] | None = None
    try:
        async with database.transaction() as writer:
            await writer.execute(text(CREATE_PROBE))
        event.listen(engine, "after_cursor_execute", pause_at_final_guard)
        event.listen(engine, "commit", observe_commit_dispatch)
        checkouts_before_write = ledger.checkouts

        task = asyncio.create_task(stage_write())
        await asyncio.wait_for(commit_started.wait(), CONDITION_DEADLINE_SECONDS)
        assert ledger.checkouts == checkouts_before_write + 1
        if interrupt == "cancel":
            loop = asyncio.get_running_loop()
            if postgres:
                held_checkouts = ledger.checkouts
                snapshot = asyncio.create_task(
                    read_probe_snapshot(
                        database, loop.time() + REFUSAL_DEADLINE_SECONDS
                    )
                )
                await until(lambda: database.units_in_flight() == 2)
                done, _ = await asyncio.wait(
                    {snapshot}, timeout=CONDITION_DEADLINE_SECONDS
                )
                assert done == {snapshot}
                with pytest.raises(DatabaseSnapshotRejected) as refused:
                    await snapshot
                assert refused.value.reason is DatabaseSnapshotFailure.DEADLINE
                assert ledger.checkouts == held_checkouts
            else:
                _, snapshot_mode, before_commit = await read_probe_snapshot(
                    database, loop.time() + CONDITION_DEADLINE_SECONDS
                )
                assert snapshot_mode == (1,)
                assert before_commit == []
            task.cancel()
            cancellation_turn = asyncio.Event()
            asyncio.get_running_loop().call_soon(cancellation_turn.set)
            await asyncio.wait_for(cancellation_turn.wait(), CONDITION_DEADLINE_SECONDS)
        else:
            await database.close(deadline_seconds=0)
        outstanding_during_interruption = ledger.outstanding
        held_during_interruption = database.units_in_flight()
        task_still_parked = not task.done()
        dispatches_while_parked = commit_dispatches
        native_still_active = all(
            driver.is_in_transaction() for driver in guard_drivers
        )
        release.set()
        if interrupt == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        elif postgres:
            with pytest.raises(DatabaseClosedError):
                await task
        else:
            await task
        native_released = all(
            not driver.is_in_transaction() for driver in guard_drivers
        )
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if snapshot is not None:
            await asyncio.gather(snapshot, return_exceptions=True)
        await database.close()

    reader_database = Database(settings)
    try:
        async with reader_database.read_transaction() as reader:
            ids = (await reader.execute(text(READ_PROBE))).scalars().all()
    finally:
        await reader_database.close()

    assert task_still_parked
    assert outstanding_during_interruption == held_during_interruption == 1
    assert native_still_active
    assert native_released
    if postgres:
        first, last = guard_drivers
        assert first is last
        assert_native_closed(last)
    else:
        assert guard_drivers == []
        (sqlite_handle,) = sqlite_handles
        assert_native_closed(sqlite_handle)
    assert dispatches_while_parked == (0 if postgres else 1)
    committed = interrupt == "cancel" or not postgres
    assert commit_dispatches == int(committed)
    assert ids == ([7] if committed else [])
    assert (
        ledger.opened
        == ledger.closed
        == (2 if not postgres and interrupt == "cancel" else 1)
    )
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
    assert database.units_in_flight() == 0
