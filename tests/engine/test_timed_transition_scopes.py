import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.conformance.capture import captured
from kora.db.connection import Database, DatabaseWriterRejected, Dialect
from kora.db.transactions import WriterTransactionError
from kora.engine.locks import (
    LockError,
    Scope,
    ScopeDeadlineExpired,
    ScopeKind,
    TimedScopeLockFailed,
    TransitionLocks,
)
from kora.observability import Level, LoggingSettings
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    ConnectionLedger,
    narrow_settings,
    until,
)
from tests.db.test_graph_read_admission import executed_queries
from tests.db.test_postgres_writer_settings import postgres_driver

CREATE_PROBE: str = "CREATE TABLE scope_deadline_probe (id INTEGER PRIMARY KEY)"
INSERT_PROBE: str = "INSERT INTO scope_deadline_probe VALUES (17)"
READ_PROBE: str = "SELECT id FROM scope_deadline_probe"
EARLIER = Scope(ScopeKind.NAMESPACE, ("timed-earlier-scope",))
LATER = Scope(ScopeKind.GRAPH, ("timed-later-scope",))


@dataclass(frozen=True)
class Backend:
    pid: int
    started: datetime


async def backend(connection: AsyncConnection) -> Backend:
    row: tuple[object, ...] = tuple(
        (
            await connection.execute(
                text(
                    "SELECT pid, backend_start FROM pg_catalog.pg_stat_activity "
                    "WHERE pid = pg_catalog.pg_backend_pid()"
                )
            )
        ).one()
    )
    pid, started = row
    assert type(pid) is int
    assert isinstance(started, datetime)
    return Backend(pid, started)


async def current_statement_timeout(connection: AsyncConnection) -> int:
    value: object = await connection.scalar(
        text(
            "SELECT setting::pg_catalog.int4 FROM pg_catalog.pg_settings "
            "WHERE name = 'statement_timeout'"
        )
    )
    assert type(value) is int
    return value


async def observe_postgresql_wait(
    database: Database, waiter: Backend, holder: Backend
) -> None:
    async with asyncio.timeout(CONDITION_DEADLINE_SECONDS):
        while True:
            async with database.read_transaction() as observer:
                waiting = await observer.scalar(
                    text(
                        "SELECT :holder = ANY(pg_catalog.pg_blocking_pids(pid)) "
                        "FROM pg_catalog.pg_stat_activity "
                        "WHERE pid = :waiter AND backend_start = :started "
                        "AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                    ),
                    {
                        "holder": holder.pid,
                        "waiter": waiter.pid,
                        "started": waiter.started,
                    },
                )
            if waiting is True:
                return


async def verify_settled_postgresql_backend(
    database: Database, waiter: Backend
) -> None:
    async with asyncio.timeout(CONDITION_DEADLINE_SECONDS):
        while True:
            async with database.read_transaction() as observer:
                present = await observer.scalar(
                    text(
                        "SELECT state = 'idle' AND xact_start IS NULL "
                        "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_locks "
                        "WHERE pid = :pid) "
                        "FROM pg_catalog.pg_stat_activity "
                        "WHERE pid = :pid AND backend_start = :started"
                    ),
                    {"pid": waiter.pid, "started": waiter.started},
                )
            if present is None or present is True:
                return


async def cross_clock_boundary(delay: float) -> None:
    clock = asyncio.get_running_loop()
    boundary = asyncio.Event()
    scheduled = clock.call_at(clock.time() + delay, boundary.set)
    try:
        await boundary.wait()
    finally:
        scheduled.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("stricter", [False, True])
async def test_timed_scopes_preserve_order_and_previous_timeout(
    database: Database, stricter: bool
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    first = Scope(ScopeKind.NAMESPACE, ("scope-deadline-namespace",))
    second = Scope(ScopeKind.GRAPH, ("scope-deadline-graph",))
    original = 600 if stricter else 5000
    ledger = ConnectionLedger(database)
    postgres = database.dialect is Dialect.POSTGRESQL
    restored: int | None = None
    prior_session: int | None = None
    after_commit: int | None = None
    original_backend: Backend | None = None
    reused_backend: Backend | None = None

    async with database.transaction(admission_deadline=deadline) as writer:
        await writer.exec_driver_sql(CREATE_PROBE)
        if postgres:
            original_backend = await backend(writer)
            prior_session = await current_statement_timeout(writer)
            await writer.scalar(
                text("SELECT pg_catalog.set_config('statement_timeout', :value, true)"),
                {"value": str(original)},
            )
        locks = TransitionLocks(writer)
        with executed_queries(writer) as queries:
            await locks.acquire_scopes((second, first, first), deadline=deadline)
        locks.require(None, (first, second))
        if postgres:
            restored = await current_statement_timeout(writer)
        await writer.exec_driver_sql(INSERT_PROBE)
    async with database.read_transaction() as reader:
        values = (await reader.exec_driver_sql(READ_PROBE)).scalars().all()
        if postgres:
            reused_backend = await backend(reader)
            after_commit = await current_statement_timeout(reader)

    if postgres:
        submitted = [
            args for statement, args in queries if "pg_advisory_xact_lock" in statement
        ]
        armed = [args[0] for statement, args in queries if "set_config" in statement]
        assert submitted == [first.key, second.key]
        assert len(queries) == 7
        assert len(armed) == 3
        assert armed[-1] == str(original)
        for value in armed[:-1]:
            assert type(value) is str
            assert (int(value) == 600) if stricter else (0 < int(value) < 2000)
        assert restored == original
        assert reused_backend == original_backend
        assert after_commit == prior_session
    else:
        assert queries == []
    assert values == [17]
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_expired_scopes_leave_a_committable_writer_for_owner_rollback(
    database: Database,
) -> None:
    loop = asyncio.get_running_loop()
    scope = Scope(ScopeKind.GRAPH, ("scope-refusal",))
    engine = database.writer_engine.sync_engine
    committed = 0
    expired = asyncio.Event()
    ledger = ConnectionLedger(database)
    queries: list[tuple[str, tuple[object, ...]]] = []

    def record_commit(connection: Connection) -> None:
        nonlocal committed
        committed += 1

    async with database.transaction() as writer:
        await writer.exec_driver_sql(CREATE_PROBE)
    event.listen(engine, "commit", record_commit)
    cutoff = loop.time() + 0.4
    marker = loop.call_at(cutoff, expired.set)

    async def refuse_scopes() -> None:
        nonlocal queries
        async with database.transaction(admission_deadline=cutoff) as writer:
            await writer.exec_driver_sql(INSERT_PROBE)
            locks = TransitionLocks(writer)
            with executed_queries(writer) as queries:
                with pytest.raises(WriterTransactionError, match="not current"):
                    await asyncio.create_task(
                        locks.acquire_scopes((scope,), deadline=cutoff)
                    )
                await asyncio.wait_for(expired.wait(), CONDITION_DEADLINE_SECONDS)
                try:
                    await locks.acquire_scopes((scope,), deadline=cutoff)
                except ScopeDeadlineExpired:
                    with pytest.raises(LockError, match="cannot be used"):
                        locks.require(None, (scope,))
                    assert writer.in_transaction()
                    raise

    try:
        with pytest.raises(ScopeDeadlineExpired) as refused:
            await refuse_scopes()
        assert committed == 0
        async with database.read_transaction() as reader:
            values = (await reader.exec_driver_sql(READ_PROBE)).scalars().all()
    finally:
        marker.cancel()
        event.remove(engine, "commit", record_commit)

    assert refused.value.__cause__ is refused.value.__context__ is None
    assert queries == []
    assert values == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


async def wait_on_later_scope(
    database: Database,
    deadline: float,
    mode: str,
    ready: asyncio.Future[Backend],
    observed: list[tuple[str, tuple[object, ...]]],
) -> None:
    arms = 0

    def separate_submissions(
        connection: object,
        cursor: object,
        statement: str,
        parameters: tuple[object, ...],
        context: object,
        executemany: bool,
    ) -> None:
        nonlocal arms
        if "pg_advisory_xact_lock" in statement and parameters == EARLIER.key:
            await_(cross_clock_boundary(0.04))
        if "set_config" in statement:
            arms += 1
            if arms == 2:
                await_(cross_clock_boundary(0.04))

    async with database.transaction(admission_deadline=deadline) as writer:
        await writer.exec_driver_sql(INSERT_PROBE)
        if mode == "native":
            await writer.exec_driver_sql("SET LOCAL statement_timeout TO '600'")
        ready.set_result(await backend(writer))
        locks = TransitionLocks(writer)
        engine_connection = writer.sync_connection
        if mode == "deadline":
            event.listen(
                engine_connection, "after_cursor_execute", separate_submissions
            )
        try:
            with executed_queries(writer) as queries:
                try:
                    await locks.acquire_scopes((LATER, EARLIER), deadline=deadline)
                finally:
                    observed.extend(queries)
            raise AssertionError("the contended scope set was handed off")
        finally:
            if mode == "deadline":
                event.remove(
                    engine_connection, "after_cursor_execute", separate_submissions
                )


async def sqlite_contention_control(database: Database, mode: str) -> None:
    loop = asyncio.get_running_loop()
    cutoff = loop.time() + (0.4 if mode == "deadline" else CONDITION_DEADLINE_SECONDS)

    async def contender() -> None:
        async with database.transaction(admission_deadline=cutoff) as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_scopes((LATER, EARLIER), deadline=cutoff)
            await writer.exec_driver_sql(INSERT_PROBE)

    task: asyncio.Task[None] | None = None
    try:
        async with database.transaction() as holder:
            locked = TransitionLocks(holder)
            await locked.acquire_scopes((LATER,))
            task = asyncio.create_task(contender())
            await until(lambda: database.units_in_flight() == 2)
            if mode == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(
                        asyncio.shield(task), CONDITION_DEADLINE_SECONDS
                    )
            elif mode == "deadline":
                with pytest.raises(DatabaseWriterRejected):
                    await asyncio.wait_for(
                        asyncio.shield(task), CONDITION_DEADLINE_SECONDS
                    )
            locked.require(None, (LATER,))
            assert database.units_in_flight() == (2 if mode == "native" else 1)
        if mode == "native":
            await asyncio.wait_for(asyncio.shield(task), CONDITION_DEADLINE_SECONDS)
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(task, return_exceptions=True)),
                CONDITION_DEADLINE_SECONDS,
            )
    async with database.read_transaction() as reader:
        values = (await reader.exec_driver_sql(READ_PROBE)).scalars().all()

    assert values == ([17] if mode == "native" else [])
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deadline", "native", "cancel"])
async def test_timed_scope_contention_releases_partial_owned_set(
    database: Database, mode: str
) -> None:
    loop = asyncio.get_running_loop()
    async with database.transaction() as writer:
        await writer.exec_driver_sql(CREATE_PROBE)
    if database.dialect is Dialect.SQLITE:
        await sqlite_contention_control(database, mode)
        return
    candidate = Database(narrow_settings(database.settings, pool_size=1))
    ledger = ConnectionLedger(candidate)
    observed: list[tuple[str, tuple[object, ...]]] = []
    ready: asyncio.Future[Backend] = loop.create_future()
    task: asyncio.Task[None] | None = None
    commits = 0

    def record_commit(connection: Connection) -> None:
        nonlocal commits
        commits += 1

    event.listen(candidate.writer_engine.sync_engine, "commit", record_commit)
    try:
        async with database.transaction() as holder:
            lock = TransitionLocks(holder)
            await lock.acquire_scopes((LATER,))
            blocker = await backend(holder)
            cutoff = loop.time() + (
                1.5 if mode == "deadline" else CONDITION_DEADLINE_SECONDS
            )
            task = asyncio.create_task(
                wait_on_later_scope(candidate, cutoff, mode, ready, observed)
            )
            original = await asyncio.wait_for(ready, CONDITION_DEADLINE_SECONDS)
            await observe_postgresql_wait(database, original, blocker)
            if mode == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await asyncio.wait_for(
                        asyncio.shield(task), CONDITION_DEADLINE_SECONDS
                    )
                assert cancelled.value.__cause__ is cancelled.value.__context__ is None
            else:
                expected = (
                    ScopeDeadlineExpired if mode == "deadline" else TimedScopeLockFailed
                )
                with pytest.raises(expected) as refused:
                    await asyncio.wait_for(
                        asyncio.shield(task), CONDITION_DEADLINE_SECONDS
                    )
                assert refused.value.__cause__ is refused.value.__context__ is None
            assert commits == 0
            assert candidate.units_in_flight() == ledger.outstanding == 0
            await verify_settled_postgresql_backend(database, original)
            async with candidate.transaction() as replacement:
                free = await replacement.scalar(
                    text(
                        "SELECT pg_catalog.pg_try_advisory_xact_lock(:kind, :identity)"
                    ),
                    dict(zip(("kind", "identity"), EARLIER.key, strict=True)),
                )
                values = (await replacement.exec_driver_sql(READ_PROBE)).scalars().all()
                await replacement.exec_driver_sql(INSERT_PROBE)
            assert free is True
            assert values == []
            assert await backend(holder) == blocker
            lock.require(None, (LATER,))
        async with database.read_transaction() as reader:
            committed = (await reader.exec_driver_sql(READ_PROBE)).scalars().all()
    finally:
        try:
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.wait_for(
                    asyncio.shield(asyncio.gather(task, return_exceptions=True)),
                    CONDITION_DEADLINE_SECONDS,
                )
        finally:
            await candidate.close()

    keys = [
        params for statement, params in observed if "pg_advisory_xact_lock" in statement
    ]
    armed = [params[0] for statement, params in observed if "set_config" in statement]
    assert keys == [EARLIER.key, LATER.key]
    assert len(observed) == 5
    assert len(armed) == 2
    if mode == "deadline":
        first, second = armed
        assert type(first) is str
        assert type(second) is str
        assert 0 < int(second) < int(first) <= 1500
    elif mode == "native":
        assert armed == ["600", "600"]
    assert committed == [17]
    assert ledger.opened == ledger.closed
    assert ledger.outstanding == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("late_phase", ["arm", "readback"])
async def test_late_scope_setup_never_hands_off_a_committable_writer(
    database: Database, late_phase: str
) -> None:
    loop = asyncio.get_running_loop()
    ledger = ConnectionLedger(database)
    postgres = database.dialect is Dialect.POSTGRESQL
    consumed = asyncio.Event()
    blocked = asyncio.Event()
    reads = 0
    queries: list[tuple[str, tuple[object, ...]]] = []

    async def return_after_timer() -> None:
        try:
            await asyncio.wait_for(blocked.wait(), CONDITION_DEADLINE_SECONDS)
        except asyncio.CancelledError:
            consumed.set()

    def hold_final_readback(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        nonlocal reads
        if "FROM pg_catalog.pg_settings" in statement:
            reads += 1
            if late_phase == "readback" and reads == 2:
                await_(return_after_timer())
        if late_phase == "arm" and "set_config" in statement:
            await_(return_after_timer())

    async with database.transaction() as writer:
        await writer.exec_driver_sql(CREATE_PROBE)
    cutoff = loop.time() + 0.6

    async def refuse_after_native_work() -> None:
        nonlocal queries
        async with database.transaction(admission_deadline=cutoff) as writer:
            await writer.exec_driver_sql(INSERT_PROBE)
            locks = TransitionLocks(writer)
            sync_connection = writer.sync_connection
            if postgres:
                event.listen(
                    sync_connection, "after_cursor_execute", hold_final_readback
                )
            else:
                await cross_clock_boundary(max(0.0, cutoff - loop.time()))
            try:
                with executed_queries(writer) as queries:
                    try:
                        await locks.acquire_scopes((EARLIER,), deadline=cutoff)
                    except ScopeDeadlineExpired:
                        assert writer.in_transaction()
                        if postgres:
                            raw = await writer.get_raw_connection()
                            assert postgres_driver(
                                raw.driver_connection
                            ).is_in_transaction()
                        with pytest.raises(LockError, match="cannot be used"):
                            locks.require(None, (EARLIER,))
                        raise
            finally:
                if postgres:
                    event.remove(
                        sync_connection, "after_cursor_execute", hold_final_readback
                    )

    with captured(
        "engine",
        LoggingSettings(level=Level.DEBUG),
        "scope-test",
        lambda: datetime.now(UTC),
    ) as capture:
        with pytest.raises(ScopeDeadlineExpired) as refused:
            await refuse_after_native_work()
        records = capture.finish()
    async with database.read_transaction() as reader:
        values = (await reader.exec_driver_sql(READ_PROBE)).scalars().all()

    assert consumed.is_set() is postgres
    if postgres:
        assert reads == (2 if late_phase == "readback" else 1)
        assert len(queries) == (5 if late_phase == "readback" else 2)
        if late_phase == "arm":
            assert all("pg_advisory_xact_lock" not in sql for sql, _ in queries)
    else:
        assert reads == 0
        assert queries == []
    assert str(refused.value) == "the scope lock deadline expired"
    assert all(record["event"] != "engine.locks.acquired" for record in records)
    assert "scope_deadline_probe" not in json.dumps(records)
    assert values == []
    assert ledger.outstanding == database.units_in_flight() == 0
