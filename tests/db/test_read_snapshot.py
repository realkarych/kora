import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import SQLITE_READONLY
from traceback import format_exception, walk_tb
from typing import Protocol, cast

import pytest
import pytest_asyncio
from sqlalchemy import Connection, event, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_
from sqlalchemy.util.concurrency import greenlet_spawn

from kora.conformance.capture import captured
from kora.db.connection import (
    SQLITE_READERS,
    Database,
    DatabaseClosedError,
    DatabaseSettings,
    DatabaseSnapshotFailure,
    DatabaseSnapshotRejected,
    Dialect,
    Lifecycle,
)
from kora.db.tables import namespace
from kora.observability import Level, LoggingSettings
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    REFUSAL_DEADLINE_SECONDS,
    ConnectionLedger,
    narrow_settings,
    until,
)
from tests.db.seed import Seeder
from tests.db.test_sqlite_writer_entry import sqlite_driver


class PostgresNativeConnection(Protocol):
    def is_closed(self) -> bool: ...
    def terminate(self) -> None: ...


def physical_handle(database: Database, driver: object) -> object:
    if database.dialect is Dialect.SQLITE:
        handle: object = vars(sqlite_driver(driver))["_connection"]
        assert isinstance(handle, sqlite3.Connection)
        return handle
    return driver


def assert_native_closed(handle: object) -> None:
    if isinstance(handle, sqlite3.Connection):
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            handle.execute("SELECT 1")
    else:
        assert cast(PostgresNativeConnection, handle).is_closed()


@pytest_asyncio.fixture
async def single_slot_database(
    database_settings: DatabaseSettings,
) -> AsyncIterator[Database]:
    database = Database(narrow_settings(database_settings, pool_size=1))
    try:
        yield database
    finally:
        await database.close()


async def seed(database: Database) -> str:
    async with database.transaction() as connection:
        return await Seeder(connection).namespace()


async def revision(connection: AsyncConnection, namespace_id: str) -> int:
    value: object = await connection.scalar(
        select(namespace.c.policy_revision).where(namespace.c.id == namespace_id)
    )
    assert type(value) is int
    return value


async def commit_revision(database: Database, namespace_id: str, value: int) -> None:
    async with database.transaction() as connection:
        await connection.execute(
            update(namespace)
            .where(namespace.c.id == namespace_id)
            .values(policy_revision=value)
        )


async def ordinary_session(
    database: Database, namespace_id: str
) -> tuple[int, tuple[object, ...]]:
    async with database.read_transaction() as connection:
        return await revision(connection, namespace_id), await settings(
            connection, database.dialect
        )


async def settings(connection: AsyncConnection, dialect: Dialect) -> tuple[object, ...]:
    if dialect is Dialect.SQLITE:
        row = (await connection.exec_driver_sql("PRAGMA query_only")).one()
    else:
        row = (
            await connection.exec_driver_sql(
                "SELECT pg_backend_pid(), current_setting('transaction_isolation'), "
                "current_setting('transaction_read_only')"
            )
        ).one()
    return tuple(row)


def assert_snapshot_settings(
    dialect: Dialect, ordinary: tuple[object, ...], snapshot: tuple[object, ...]
) -> None:
    if dialect is Dialect.SQLITE:
        assert ordinary == snapshot == (1,)
    else:
        pid = ordinary[0]
        assert ordinary == (pid, "read committed", "off")
        assert snapshot == (pid, "repeatable read", "on")


@pytest.mark.asyncio
@pytest.mark.parametrize("repeatable", [False, True], ids=["ordinary", "snapshot"])
async def test_metadata_read_visibility_after_an_intervening_commit(
    database: Database, repeatable: bool
) -> None:
    namespace_id = await seed(database)
    ledger = ConnectionLedger(database)
    read = database.read_snapshot if repeatable else database.read_transaction

    async with read() as connection:
        before = await revision(connection, namespace_id)
        await asyncio.wait_for(
            commit_revision(database, namespace_id, 2), CONDITION_DEADLINE_SECONDS
        )
        during = await revision(connection, namespace_id)
    after, _ = await ordinary_session(database, namespace_id)

    assert before == 1
    assert during == (1 if repeatable else 2)
    assert after == 2
    assert ledger.checkouts == 3
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_snapshot_refuses_native_writes_and_restores_ordinary_session(
    single_slot_database: Database,
) -> None:
    database = single_slot_database
    namespace_id = await seed(database)
    ledger = ConnectionLedger(database)
    before = await ordinary_session(database, namespace_id)

    async with database.read_snapshot() as connection:
        configured = await settings(connection, database.dialect)
    after_success = await ordinary_session(database, namespace_id)
    with pytest.raises(DBAPIError) as refusal:
        async with database.read_snapshot() as connection:
            await connection.execute(
                update(namespace)
                .where(namespace.c.id == namespace_id)
                .values(policy_revision=2)
            )
    after_refusal = await ordinary_session(database, namespace_id)
    await commit_revision(database, namespace_id, 3)
    after_write = await ordinary_session(database, namespace_id)

    assert_snapshot_settings(database.dialect, before[1], configured)
    assert before == after_success == after_refusal == (1, before[1])
    assert after_write == (3, before[1])
    if database.dialect is Dialect.SQLITE:
        assert getattr(refusal.value.orig, "sqlite_errorcode", None) == SQLITE_READONLY
    else:
        assert getattr(refusal.value.orig, "sqlstate", None) == "25006"
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("during_setup", [False, True], ids=["body", "setup"])
async def test_snapshot_cancellation_releases_the_transaction_and_lease(
    single_slot_database: Database, during_setup: bool
) -> None:
    database = single_slot_database
    namespace_id = await seed(database)
    ledger = ConnectionLedger(database)
    before = await ordinary_session(database, namespace_id)
    setup_ran = asyncio.Event()
    release_setup = asyncio.Event()
    setup_settled = asyncio.Event()
    body_entered = asyncio.Event()
    body_wait = asyncio.Event()
    rollback_after_setup: list[bool] = []
    setup = (
        "BEGIN DEFERRED"
        if database.dialect is Dialect.SQLITE
        else "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )

    def after_setup(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement == setup:
            setup_ran.set()
            await_(release_setup.wait())
            setup_settled.set()

    def on_rollback(connection: object) -> None:
        rollback_after_setup.append(setup_settled.is_set())

    async def read() -> None:
        async with database.read_snapshot() as connection:
            await revision(connection, namespace_id)
            body_entered.set()
            await body_wait.wait()

    if during_setup:
        event.listen(
            database.reader_engine.sync_engine, "after_cursor_execute", after_setup
        )
    event.listen(database.reader_engine.sync_engine, "rollback", on_rollback)
    barrier = setup_ran if during_setup else body_entered

    async with asyncio.TaskGroup() as group:
        task = group.create_task(read())
        try:
            await asyncio.wait_for(barrier.wait(), CONDITION_DEADLINE_SECONDS)
            assert task.cancel()
            if during_setup:
                cancellation_turn = asyncio.Event()
                asyncio.get_running_loop().call_soon(cancellation_turn.set)
                await cancellation_turn.wait()
                assert not task.done()
                assert ledger.outstanding == database.units_in_flight() == 1
                assert not setup_settled.is_set()
        finally:
            release_setup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
    checked_in = ledger.outstanding
    in_flight = database.units_in_flight()
    rollbacks = tuple(rollback_after_setup)
    await commit_revision(database, namespace_id, 2)
    after = await ordinary_session(database, namespace_id)

    assert body_entered.is_set() is not during_setup
    assert setup_settled.is_set() is during_setup
    assert rollbacks == (during_setup,)
    assert (
        checked_in == in_flight == ledger.outstanding == database.units_in_flight() == 0
    )
    assert after == (2, before[1])


async def timed_snapshot(database: Database, deadline: float) -> int:
    async with database.read_snapshot(deadline=deadline) as connection:
        value: object = await connection.scalar(select(1))
        assert type(value) is int
        return value


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["deadline", "cancel"])
async def test_timed_snapshot_wait_exits_without_a_late_checkout(
    database_settings: DatabaseSettings, cancel: bool
) -> None:
    settings = replace(narrow_settings(database_settings, pool_size=1), max_overflow=1)
    database = Database(settings)
    ledger = ConnectionLedger(database)
    readers = SQLITE_READERS if database.dialect is Dialect.SQLITE else 1
    held = AsyncExitStack()
    try:
        for _ in range(readers):
            await held.enter_async_context(database.read_transaction())
        await held.enter_async_context(database.transaction())
        holders = readers + 1
        deadline = asyncio.get_running_loop().time() + REFUSAL_DEADLINE_SECONDS
        subsequent_deadline = (
            asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
        )

        queued = asyncio.create_task(timed_snapshot(database, deadline))
        successor = asyncio.create_task(timed_snapshot(database, subsequent_deadline))
        try:
            await until(lambda: database.units_in_flight() == holders + 2)
            if cancel:
                assert queued.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(queued, CONDITION_DEADLINE_SECONDS)
            else:
                with pytest.raises(DatabaseSnapshotRejected) as refused:
                    await asyncio.wait_for(queued, CONDITION_DEADLINE_SECONDS)
                assert refused.value.reason is DatabaseSnapshotFailure.DEADLINE
            outstanding = ledger.outstanding
            checkouts = ledger.checkouts
            units = database.units_in_flight()
            successor_waiting = not successor.done()
        finally:
            await held.aclose()
        observed = await asyncio.wait_for(successor, CONDITION_DEADLINE_SECONDS)
    finally:
        await held.aclose()
        await database.close()

    assert outstanding == checkouts == holders
    assert units == holders + 1
    assert successor_waiting
    assert observed == 1
    assert ledger.checkouts == holders + 1
    assert ledger.outstanding == database.units_in_flight() == 0
    assert ledger.opened == ledger.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["connect", "setup"])
@pytest.mark.parametrize("cancel", [False, True], ids=["deadline", "cancel"])
async def test_timed_snapshot_retains_an_entered_native_operation_until_release(
    single_slot_database: Database, phase: str, cancel: bool
) -> None:
    database = single_slot_database
    ledger = ConnectionLedger(database)
    engine = database.reader_engine.sync_engine
    entered = asyncio.Event()
    allow_finish = asyncio.Event()
    settled = asyncio.Event()
    setup = (
        "BEGIN DEFERRED"
        if database.dialect is Dialect.SQLITE
        else "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )

    def pause() -> None:
        if not entered.is_set():
            entered.set()
            await_(allow_finish.wait())
            settled.set()

    def pause_connection(*arguments: object) -> None:
        pause()

    def pause_setup(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement == setup:
            pause()

    if phase == "connect":
        event.listen(engine, "connect", pause_connection)
    else:
        event.listen(engine, "after_cursor_execute", pause_setup)
    interval = CONDITION_DEADLINE_SECONDS if cancel else REFUSAL_DEADLINE_SECONDS
    deadline = asyncio.get_running_loop().time() + interval

    task = asyncio.create_task(timed_snapshot(database, deadline))
    try:
        try:
            await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)
            if cancel:
                assert task.cancel()
            await until(lambda: task.cancelling() > 0)
            pending = not task.done()
            not_settled = not settled.is_set()
            still_owned = database.units_in_flight()
            native_checked_out = ledger.outstanding
        finally:
            allow_finish.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
        else:
            with pytest.raises(DatabaseSnapshotRejected) as refused:
                await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
            assert refused.value.reason is DatabaseSnapshotFailure.DEADLINE
    finally:
        if phase == "connect":
            event.remove(engine, "connect", pause_connection)
        else:
            event.remove(engine, "after_cursor_execute", pause_setup)
    after_release = (database.units_in_flight(), ledger.outstanding)
    reused = await timed_snapshot(
        database, asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
    )
    await database.close()

    assert pending
    assert not_settled
    assert still_owned == 1
    assert native_checked_out == (0 if phase == "connect" else 1)
    assert settled.is_set()
    assert after_release == (0, 0)
    assert reused == 1
    assert ledger.checkouts == 2
    assert ledger.opened == ledger.closed == 1


@pytest.mark.asyncio
async def test_ordinary_pending_acquisition_claims_its_native_slot(
    single_slot_database: Database,
) -> None:
    database = single_slot_database
    ledger = ConnectionLedger(database)
    engine = database.reader_engine.sync_engine
    pending = asyncio.Event()
    finish_connect = asyncio.Event()
    body_entered = asyncio.Event()
    hold_body = asyncio.Event()
    capacity = SQLITE_READERS if database.dialect is Dialect.SQLITE else 1
    held = AsyncExitStack()

    def pause_connection(*arguments: object) -> None:
        pending.set()
        await_(finish_connect.wait())

    async def ordinary() -> None:
        unit = (
            database.read_transaction
            if database.dialect is Dialect.SQLITE
            else database.migration_connection
        )
        async with unit():
            body_entered.set()
            await hold_body.wait()

    async with held:
        for _ in range(capacity - 1):
            await held.enter_async_context(database.read_transaction())
        event.listen(engine, "connect", pause_connection)
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(ordinary())
                try:
                    await asyncio.wait_for(pending.wait(), CONDITION_DEADLINE_SECONDS)
                    deadline = (
                        asyncio.get_running_loop().time() + REFUSAL_DEADLINE_SECONDS
                    )

                    with pytest.raises(DatabaseSnapshotRejected) as refused:
                        await timed_snapshot(database, deadline)
                    while_pending = (ledger.checkouts, database.units_in_flight())
                finally:
                    finish_connect.set()
                    hold_body.set()
        finally:
            event.remove(engine, "connect", pause_connection)
    observed = await timed_snapshot(
        database, asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
    )
    await database.close()

    assert refused.value.reason is DatabaseSnapshotFailure.DEADLINE
    assert while_pending == (capacity - 1, capacity)
    assert body_entered.is_set()
    assert observed == 1
    assert ledger.outstanding == database.units_in_flight() == 0
    assert ledger.checkouts == capacity + 1
    assert ledger.opened == ledger.closed == capacity


@pytest.mark.asyncio
async def test_database_close_wakes_queued_snapshots_without_native_entry(
    single_slot_database: Database,
) -> None:
    database = single_slot_database
    ledger = ConnectionLedger(database)
    capacity = SQLITE_READERS if database.dialect is Dialect.SQLITE else 1
    release = asyncio.Event()
    deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
    handles: list[object] = []

    async def hold_reader(entered: asyncio.Event) -> None:
        async with database.read_transaction() as connection:
            raw = await connection.get_raw_connection()
            handles.append(physical_handle(database, raw.driver_connection))
            entered.set()
            await release.wait()

    async def hold(entered: asyncio.Event) -> None:
        with pytest.raises(DatabaseClosedError):
            await hold_reader(entered)

    async with asyncio.TaskGroup() as group:
        for _ in range(capacity):
            entered = asyncio.Event()
            group.create_task(hold(entered))
            await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)
        waiters = [
            asyncio.create_task(timed_snapshot(database, deadline)) for _ in range(2)
        ]

        try:
            await until(lambda: database.units_in_flight() == capacity + len(waiters))
            await database.close(deadline_seconds=0.0)
            reasons: list[DatabaseSnapshotFailure] = []
            for waiter in waiters:
                with pytest.raises(DatabaseSnapshotRejected) as refused:
                    await asyncio.wait_for(waiter, CONDITION_DEADLINE_SECONDS)
                reasons.append(refused.value.reason)
            while_held = (
                ledger.checkouts,
                ledger.outstanding,
                database.units_in_flight(),
            )
        finally:
            release.set()

    assert reasons == [DatabaseSnapshotFailure.UNAVAILABLE] * 2
    assert while_held == (capacity, capacity, capacity)
    assert ledger.opened == ledger.closed == capacity
    assert ledger.outstanding == database.units_in_flight() == 0
    for handle in handles:
        assert_native_closed(handle)


@pytest.mark.asyncio
async def test_timed_snapshot_can_settle_during_graceful_shutdown(
    single_slot_database: Database,
) -> None:
    database = single_slot_database
    ledger = ConnectionLedger(database)
    deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
    entered = asyncio.Event()
    resume = asyncio.Event()
    handles: list[object] = []

    async def read() -> None:
        async with database.read_snapshot(deadline=deadline) as connection:
            raw = await connection.get_raw_connection()
            handles.append(physical_handle(database, raw.driver_connection))
            entered.set()
            await resume.wait()

    async with asyncio.TaskGroup() as group:
        snapshot = group.create_task(read())
        try:
            await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)
            group.create_task(database.close(CONDITION_DEADLINE_SECONDS))
            await until(lambda: database.lifecycle is Lifecycle.CLOSING)
        finally:
            resume.set()

    assert snapshot.result() is None
    assert database.lifecycle is Lifecycle.CLOSED
    assert ledger.outstanding == database.units_in_flight() == 0
    assert len(handles) == ledger.opened == ledger.closed == 1
    assert_native_closed(handles[0])


@pytest.mark.asyncio
async def test_timed_snapshot_refuses_replaced_pool_before_lending(
    single_slot_database: Database,
) -> None:
    database = single_slot_database
    engine = database.reader_engine.sync_engine
    original = engine.pool
    entered = asyncio.Event()
    lent = asyncio.Event()
    resume = asyncio.Event()
    handles: list[object] = []
    deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
    setup = (
        "BEGIN DEFERRED"
        if database.dialect is Dialect.SQLITE
        else "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )

    def pause_setup(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement == setup:
            assert isinstance(connection, Connection)
            handles.append(
                physical_handle(database, connection.connection.driver_connection)
            )
            entered.set()
            await_(resume.wait())

    async def read() -> None:
        async with database.read_snapshot(deadline=deadline):
            lent.set()

    event.listen(engine, "after_cursor_execute", pause_setup)
    task = asyncio.create_task(read())
    try:
        try:
            await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)
            await database.reader_engine.dispose()
        finally:
            resume.set()
        with pytest.raises(DatabaseSnapshotRejected) as refused:
            await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
        assert len(handles) == 1
        assert_native_closed(handles[0])
    finally:
        event.remove(engine, "after_cursor_execute", pause_setup)
        await greenlet_spawn(original.dispose)
    async with database.read_transaction() as connection:
        ordinary: object = await connection.scalar(select(1))

    assert refused.value.reason is DatabaseSnapshotFailure.UNAVAILABLE
    assert not lent.is_set()
    assert ordinary == 1
    assert original is not engine.pool
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_timed_snapshots_can_use_two_freed_slots_during_stalled_checkout(
    database_settings: DatabaseSettings,
) -> None:
    database = Database(narrow_settings(database_settings, pool_size=2))
    engine = database.reader_engine.sync_engine
    capacity = SQLITE_READERS if database.dialect is Dialect.SQLITE else 2
    ledger = ConnectionLedger(database)
    first_two_release = asyncio.Event()
    other_release = asyncio.Event()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    finish_checkout = asyncio.Event()
    deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS

    async def hold(entered: asyncio.Event, release: asyncio.Event) -> None:
        async with database.read_transaction():
            entered.set()
            await release.wait()

    def after_checkout(*arguments: object) -> None:
        if not first_started.is_set():
            first_started.set()
            await_(finish_checkout.wait())
        else:
            second_started.set()

    try:
        async with asyncio.TaskGroup() as group:
            for index in range(capacity):
                entered = asyncio.Event()
                release = first_two_release if index < 2 else other_release
                group.create_task(hold(entered, release))
                await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)
            event.listen(engine, "checkout", after_checkout)
            one = group.create_task(timed_snapshot(database, deadline))
            two = group.create_task(timed_snapshot(database, deadline))
            try:
                await until(lambda: database.units_in_flight() == capacity + 2)
                first_two_release.set()
                await asyncio.wait_for(first_started.wait(), CONDITION_DEADLINE_SECONDS)
                await asyncio.wait_for(
                    second_started.wait(), CONDITION_DEADLINE_SECONDS
                )
                both_in_native = first_started.is_set() and not finish_checkout.is_set()
                checkouts = ledger.checkouts
            finally:
                first_two_release.set()
                other_release.set()
                finish_checkout.set()
    finally:
        event.remove(engine, "checkout", after_checkout)
        await database.close()

    assert both_in_native
    assert checkouts == capacity + 2
    assert one.result() == two.result() == 1
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_timed_snapshot_rolls_back_if_body_consumes_its_expiry(
    single_slot_database: Database,
) -> None:
    database = single_slot_database
    engine = database.reader_engine.sync_engine
    ready = asyncio.Event()
    deadline = asyncio.get_running_loop().time() + REFUSAL_DEADLINE_SECONDS
    endings: list[str] = []

    def on_commit(connection: object) -> None:
        endings.append("commit")

    def on_rollback(connection: object) -> None:
        endings.append("rollback")

    async def consume_expiry() -> None:
        async with database.read_snapshot(deadline=deadline):
            try:
                await ready.wait()
            except asyncio.CancelledError:
                ready.set()

    event.listen(engine, "commit", on_commit)
    event.listen(engine, "rollback", on_rollback)
    try:
        with pytest.raises(DatabaseSnapshotRejected) as refused:
            await consume_expiry()
    finally:
        event.remove(engine, "commit", on_commit)
        event.remove(engine, "rollback", on_rollback)

    assert refused.value.reason is DatabaseSnapshotFailure.DEADLINE
    assert ready.is_set()
    assert endings == ["rollback"]
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_already_requested_cancellation_starts_no_timed_native_checkout(
    single_slot_database: Database,
) -> None:
    database = single_slot_database
    ledger = ConnectionLedger(database)
    deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS

    async def cancelled_before_entry() -> int:
        task = asyncio.current_task()
        assert task is not None
        assert task.cancel()
        return await timed_snapshot(database, deadline)

    task = asyncio.create_task(cancelled_before_entry())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)

    assert ledger.checkouts == ledger.opened == ledger.outstanding == 0
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("timed", [True, False], ids=["timed", "untimed"])
async def test_snapshot_keeps_cancellation_after_native_connection_loss(
    single_slot_database: Database, timed: bool
) -> None:
    database = single_slot_database
    ledger = ConnectionLedger(database)
    entered = asyncio.Event()
    hold = asyncio.Event()
    rollback_started = asyncio.Event()
    finish_rollback = asyncio.Event()
    drivers: list[object] = []
    originals: list[asyncio.CancelledError] = []
    returned: list[asyncio.CancelledError] = []
    deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS

    async def read() -> None:
        try:
            async with database.read_snapshot(
                deadline=deadline if timed else None
            ) as connection:
                assert await connection.scalar(select(1)) == 1
                drivers.append(
                    (await connection.get_raw_connection()).driver_connection
                )
                entered.set()
                try:
                    await hold.wait()
                except asyncio.CancelledError as error:
                    originals.append(error)
                    raise
        except asyncio.CancelledError as error:
            returned.append(error)
            raise

    def pause_rollback(connection: object) -> None:
        if not rollback_started.is_set():
            rollback_started.set()
            await_(finish_rollback.wait())

    engine = database.reader_engine.sync_engine
    event.listen(engine, "rollback", pause_rollback)
    task = asyncio.create_task(read())
    try:
        try:
            await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)
            driver = drivers[0]
            handle = physical_handle(database, driver)
            if isinstance(handle, sqlite3.Connection):
                handle.close()
            else:
                cast(PostgresNativeConnection, driver).terminate()

            assert task.cancel("owner cancellation")
            await asyncio.wait_for(rollback_started.wait(), CONDITION_DEADLINE_SECONDS)
            assert ledger.outstanding == database.units_in_flight() == 1
            assert not task.done()
            assert task.cancel("repeat cancellation")
        finally:
            finish_rollback.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
        assert ledger.outstanding == database.units_in_flight() == 0
        assert ledger.closed_while_checked_out == 1
        assert_native_closed(handle)
        after_cleanup = ledger.checkouts
        probe_deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
        with pytest.raises(DatabaseSnapshotRejected) as fenced:
            await timed_snapshot(database, probe_deadline)
        after_fenced_refusal = ledger.checkouts
        async with database.read_transaction() as connection:
            recovered: object = await connection.scalar(select(1))
    finally:
        hold.set()
        event.remove(engine, "rollback", pause_rollback)
        await database.close()

    assert len(originals) == len(returned) == 1
    assert returned[0] is originals[0]
    assert returned[0].__cause__ is returned[0].__context__ is None
    assert cancelled.value.args == ("owner cancellation",)
    assert cancelled.value.__cause__ is cancelled.value.__context__ is None
    assert fenced.value.reason is DatabaseSnapshotFailure.UNAVAILABLE
    assert recovered == 1
    assert ledger.checkouts == after_fenced_refusal + 1
    assert after_fenced_refusal == after_cleanup
    assert ledger.outstanding == database.units_in_flight() == 0
    assert_native_closed(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("timed", [True, False], ids=["timed", "untimed"])
async def test_repeat_cancellation_preserves_verified_native_return(
    single_slot_database: Database, timed: bool
) -> None:
    database = single_slot_database
    ledger = ConnectionLedger(database)
    engine = database.reader_engine.sync_engine
    deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
    lent = asyncio.Event()
    hold_body = asyncio.Event()
    resetting = asyncio.Event()
    finish_reset = asyncio.Event()

    async def read() -> None:
        async with database.read_snapshot(deadline=deadline if timed else None):
            lent.set()
            await hold_body.wait()

    def pause_reset(*arguments: object) -> None:
        if not resetting.is_set():
            resetting.set()
            await_(finish_reset.wait())

    event.listen(engine, "reset", pause_reset)
    task = asyncio.create_task(read())
    try:
        try:
            await asyncio.wait_for(lent.wait(), CONDITION_DEADLINE_SECONDS)
            assert task.cancel("owner cancellation")
            await asyncio.wait_for(resetting.wait(), CONDITION_DEADLINE_SECONDS)
            assert task.cancel("repeat cancellation")
            assert ledger.outstanding == database.units_in_flight() == 1
        finally:
            hold_body.set()
            finish_reset.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
        assert ledger.outstanding == database.units_in_flight() == 0
    finally:
        event.remove(engine, "reset", pause_reset)
    verified = await timed_snapshot(database, deadline)

    assert cancelled.value.args == ("owner cancellation",)
    assert verified == 1
    assert ledger.opened == 1
    assert ledger.checkouts == 2
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("timed", [True, False], ids=["timed", "untimed"])
async def test_last_snapshot_keeps_first_cancellation_through_pool_shutdown(
    single_slot_database: Database, timed: bool
) -> None:
    database = single_slot_database
    engine = database.reader_engine.sync_engine
    deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS
    lent = asyncio.Event()
    hold_body = asyncio.Event()
    disposal = asyncio.Event()
    finish_disposal = asyncio.Event()
    handles: list[object] = []

    async def read() -> None:
        async with database.read_snapshot(
            deadline=deadline if timed else None
        ) as connection:
            raw = await connection.get_raw_connection()
            handles.append(physical_handle(database, raw.driver_connection))
            lent.set()
            await hold_body.wait()

    def pause_disposal(*arguments: object) -> None:
        if not disposal.is_set():
            disposal.set()
            await_(finish_disposal.wait())

    event.listen(engine, "close", pause_disposal)
    task = asyncio.create_task(read())
    try:
        try:
            await asyncio.wait_for(lent.wait(), CONDITION_DEADLINE_SECONDS)
            await database.close(deadline_seconds=0.0)
            assert task.cancel("owner cancellation")
            await asyncio.wait_for(disposal.wait(), CONDITION_DEADLINE_SECONDS)
            assert not task.done()
            assert task.cancel("repeat cancellation")
        finally:
            hold_body.set()
            finish_disposal.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
    finally:
        event.remove(engine, "close", pause_disposal)

    assert cancelled.value.args == ("owner cancellation",)
    assert database.lifecycle is Lifecycle.CLOSED
    assert database.units_in_flight() == 0
    assert len(handles) == 1
    assert_native_closed(handles[0])


@pytest.mark.asyncio
async def test_timed_snapshot_cold_driver_failure_is_private(
    database_settings: DatabaseSettings, tmp_path: Path
) -> None:
    private = "private-timed-snapshot-driver-source"
    target = (
        str(tmp_path / private)
        if database_settings.dialect is Dialect.SQLITE
        else private
    )
    database = Database(
        replace(database_settings, url=database_settings.url.set(database=target))
    )
    if database.dialect is Dialect.SQLITE:
        database.settings.sqlite_path.unlink()
        database.settings.sqlite_path.mkdir()
    ledger = ConnectionLedger(database)
    deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS

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
        "snapshot-test",
        lambda: datetime.now(UTC),
    ) as capture:
        try:
            with pytest.raises(DatabaseSnapshotRejected) as refused:
                await timed_snapshot(database, deadline)
        finally:
            await database.close()
        records = capture.finish()

    assert refused.value.reason is DatabaseSnapshotFailure.UNAVAILABLE
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert private not in "".join(format_exception(refused.value))
    assert private not in json.dumps(records)
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refused.value.__traceback__)
    }
    assert "_checkout" not in frames
    assert "connect_reserved" not in frames
    assert frames["read_snapshot"] == {"reason": DatabaseSnapshotFailure.UNAVAILABLE}
    assert ledger.checkouts == ledger.outstanding == database.units_in_flight() == 0


async def read_locked_metadata(database: Database, deadline: float) -> None:
    async with database.read_snapshot(deadline=deadline) as connection:
        await connection.execute(select(namespace.c.id))


@pytest.mark.asyncio
async def test_timed_snapshot_distinguishes_a_native_read_timeout(
    database_settings: DatabaseSettings,
) -> None:
    settings = database_settings
    if settings.dialect is Dialect.POSTGRESQL:
        settings = replace(
            settings, url=settings.url.update_query_dict({"command_timeout": "1.0"})
        )
    database = Database(settings)
    ledger = ConnectionLedger(database)
    deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS

    try:
        if settings.dialect is Dialect.POSTGRESQL:
            assert await timed_snapshot(database, deadline) == 1
            async with database.transaction() as writer:
                await writer.exec_driver_sql(
                    "LOCK TABLE namespace IN ACCESS EXCLUSIVE MODE"
                )
                with pytest.raises(DatabaseSnapshotRejected) as refused:
                    await read_locked_metadata(database, deadline)
        else:
            with closing(sqlite3.connect(settings.sqlite_path)) as writer:
                writer.execute("PRAGMA journal_mode=DELETE")
                async with database.read_transaction() as reader:
                    await reader.exec_driver_sql("PRAGMA busy_timeout=100")
                writer.execute("BEGIN EXCLUSIVE")
                try:
                    with pytest.raises(DatabaseSnapshotRejected) as refused:
                        await read_locked_metadata(database, deadline)
                finally:
                    writer.rollback()
        recovered = await timed_snapshot(database, deadline)
    finally:
        await database.close()

    assert refused.value.reason is DatabaseSnapshotFailure.UNAVAILABLE
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert recovered == 1
    assert ledger.outstanding == database.units_in_flight() == 0
