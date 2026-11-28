import asyncio
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Coroutine
from typing import cast

import pytest
from aiosqlite import Connection as SQLiteDriver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Dialect
from kora.db.maintenance import NATIVE_READ_GUARD, NativeReadGuard
from kora.engine.locks import TransitionLocks
from tests.db.closing import CONDITION_DEADLINE_SECONDS, sqlite_contender, until
from tests.db.native_read_support import SqliteTraceControl
from tests.engine.test_timed_transition_scopes import (
    Backend,
    backend,
    settle_scope_contender,
    verify_settled_postgresql_backend,
)

CHECKPOINT_INSERT: str = "INSERT INTO run_event_position_state"
Verifier = Callable[[AsyncConnection], Awaitable[None]]
Prepare = Callable[[Verifier], Coroutine[object, object, None]]
Observe = Callable[[], Awaitable[None]]


async def require_cancellation(task: asyncio.Task[None]) -> None:
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
    assert task.cancelled()


async def cancel_sqlite_statement(
    database: Database, prepare: Prepare, observe_paused: Observe
) -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    installed: asyncio.Future[NativeReadGuard] = loop.create_future()

    def observe_sql(statement: str) -> None:
        if statement.startswith(CHECKPOINT_INSERT) and not release.is_set():
            loop.call_soon_threadsafe(entered.set)
            release.wait(CONDITION_DEADLINE_SECONDS)

    async def verify_writer(writer: AsyncConnection) -> None:
        guard: object = writer.info.get(NATIVE_READ_GUARD)
        assert isinstance(guard, NativeReadGuard)
        driver = (await writer.get_raw_connection()).driver_connection
        assert isinstance(driver, SQLiteDriver)
        await cast(SqliteTraceControl, driver).set_trace_callback(observe_sql)
        installed.set_result(guard)

    pending = asyncio.create_task(prepare(verify_writer))
    path = database.settings.sqlite_path
    try:
        try:
            await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)
            guard = installed.result()
            assert not database.writer_is_free()
            with sqlite_contender(path) as contender:
                with pytest.raises(sqlite3.OperationalError) as conflict:
                    contender.execute("BEGIN IMMEDIATE").close()
                assert conflict.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
            await observe_paused()
            assert pending.cancel()
            await until(guard.cutoff.cancelled.is_set)
            assert database.units_in_flight() == 1
            assert not pending.done()
            assert not database.writer_is_free()
        finally:
            release.set()
        await require_cancellation(pending)
    finally:
        await settle_scope_contender(pending)

    with sqlite_contender(path) as contender:
        contender.execute("BEGIN IMMEDIATE").close()
        assert contender.in_transaction
        contender.rollback()


async def observe_checkpoint_lock(
    database: Database, waiter: Backend, holder: Backend
) -> None:
    statement = text("""
        SELECT state = 'active' AND wait_event_type = 'Lock' AND wait_event = 'relation'
            AND query LIKE :statement
            AND :holder = ANY(pg_catalog.pg_blocking_pids(pid))
        FROM pg_catalog.pg_stat_activity
        WHERE pid = :waiter AND backend_start = :started
        """)
    async with asyncio.timeout(CONDITION_DEADLINE_SECONDS):
        while True:
            async with database.read_transaction() as observer:
                waiting = await observer.scalar(
                    statement,
                    {
                        "statement": f"{CHECKPOINT_INSERT}%",
                        "holder": holder.pid,
                        "waiter": waiter.pid,
                        "started": waiter.started,
                    },
                )
            if waiting is True:
                return


async def cancel_postgresql_statement(
    database: Database, run_id: str, prepare: Prepare, observe_paused: Observe
) -> None:
    observer = Database(database.settings)
    identified: asyncio.Future[Backend] = asyncio.get_running_loop().create_future()

    async def verify_writer(writer: AsyncConnection) -> None:
        identified.set_result(await backend(writer))

    async def try_native_run_guard() -> bool:
        async with observer.transaction() as contender:
            return await TransitionLocks(contender).acquire_run(run_id, wait=False)

    try:
        async with observer.transaction() as holder:
            await holder.exec_driver_sql(
                "LOCK TABLE run_event_position_state IN SHARE MODE"
            )
            blocker = await backend(holder)
            pending = asyncio.create_task(prepare(verify_writer))
            try:
                waiter = await asyncio.wait_for(identified, CONDITION_DEADLINE_SECONDS)
                await observe_checkpoint_lock(observer, waiter, blocker)
                assert not await try_native_run_guard()
                await observe_paused()
                assert pending.cancel()
                await require_cancellation(pending)
                await verify_settled_postgresql_backend(observer, waiter)
                assert await try_native_run_guard()
            finally:
                await settle_scope_contender(pending)
    finally:
        await observer.close()


async def cancel_position_preparation(
    database: Database, run_id: str, prepare: Prepare, observe_paused: Observe
) -> None:
    if database.dialect is Dialect.SQLITE:
        await cancel_sqlite_statement(database, prepare, observe_paused)
    else:
        await cancel_postgresql_statement(database, run_id, prepare, observe_paused)
