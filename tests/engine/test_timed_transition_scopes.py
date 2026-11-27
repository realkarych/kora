import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

import pytest
from sqlalchemy import Connection, event, text
from sqlalchemy.engine.interfaces import ExceptionContext
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.conformance.capture import captured
from kora.db.connection import (
    Database,
    DatabaseSettings,
    DatabaseWriterFailure,
    DatabaseWriterRejected,
    Dialect,
)
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
    WriterCutoffLoop,
    narrow_settings,
    until,
)
from tests.db.test_graph_read_admission import executed_queries
from tests.db.test_postgres_writer_settings import postgres_driver
from tests.db.test_read_snapshot import PostgresNativeConnection

CREATE_PROBE: str = "CREATE TABLE scope_deadline_probe (id INTEGER PRIMARY KEY)"
INSERT_PROBE: str = "INSERT INTO scope_deadline_probe VALUES (17)"
READ_PROBE: str = "SELECT id FROM scope_deadline_probe"
EARLIER = Scope(ScopeKind.NAMESPACE, ("timed-earlier-scope",))
LATER = Scope(ScopeKind.GRAPH, ("timed-later-scope",))


@dataclass(frozen=True)
class Backend:
    pid: int
    started: datetime


ContenderPhase = Literal["writer_entry", "probe_insert", "native_timeout", "backend"]


def contender_outcome(task: asyncio.Task[None]) -> str:
    if not task.done():
        return "pending"
    if task.cancelled():
        return "cancelled"
    error = task.exception()
    if isinstance(error, DatabaseWriterRejected):
        if error.reason is DatabaseWriterFailure.DEADLINE:
            return "writer_deadline"
        return "writer_unavailable"
    if isinstance(error, IntegrityError):
        return "integrity_error"
    if isinstance(error, SQLAlchemyError):
        return "native_error"
    if isinstance(error, AssertionError):
        return "fixture_assertion"
    return "returned" if error is None else "raised"


class ContenderReadiness:
    def __init__(self) -> None:
        self.phase: ContenderPhase = "writer_entry"
        self.ready: asyncio.Future[Backend] = asyncio.get_running_loop().create_future()

    def diagnostic(self, task: asyncio.Task[None]) -> str:
        return (
            f"contender readiness phase={self.phase} outcome={contender_outcome(task)}"
        )

    def contender_finished(self, task: asyncio.Task[None]) -> None:
        if not self.ready.done():
            self.ready.set_exception(AssertionError(self.diagnostic(task)))

    async def wait(self, task: asyncio.Task[None]) -> Backend:
        task.add_done_callback(self.contender_finished)
        try:
            return await asyncio.wait_for(self.ready, CONDITION_DEADLINE_SECONDS)
        except TimeoutError as error:
            error.add_note(self.diagnostic(task))
            raise


async def settle_scope_contender(task: asyncio.Task[None]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.wait_for(
        asyncio.shield(asyncio.gather(task, return_exceptions=True)),
        CONDITION_DEADLINE_SECONDS,
    )


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
    database: Database, waiter: Backend, *, absent: bool | None = None
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
            if (absent is not False and present is None) or (
                absent is not True and present is True
            ):
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
    readiness: ContenderReadiness,
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
        readiness.phase = "probe_insert"
        await writer.exec_driver_sql(INSERT_PROBE)
        if mode == "native":
            readiness.phase = "native_timeout"
            await writer.exec_driver_sql("SET LOCAL statement_timeout TO '600'")
        readiness.phase = "backend"
        readiness.ready.set_result(await backend(writer))
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
    statement_timeouts: list[object] = []
    readiness = ContenderReadiness()
    task: asyncio.Task[None] | None = None
    commits = 0

    def record_commit(connection: Connection) -> None:
        nonlocal commits
        commits += 1

    def record_statement_timeout(context: ExceptionContext) -> None:
        statement = context.statement
        error = context.original_exception
        if (
            statement is not None
            and "pg_advisory_xact_lock" in statement
            and getattr(error, "sqlstate", None) == "57014"
            and "canceling statement due to statement timeout" in str(error)
        ):
            statement_timeouts.append(context.parameters)

    engine = candidate.writer_engine.sync_engine
    event.listen(engine, "commit", record_commit)
    if mode == "native":
        event.listen(engine, "handle_error", record_statement_timeout)
    try:
        async with database.transaction() as holder:
            lock = TransitionLocks(holder)
            await lock.acquire_scopes((LATER,))
            blocker = await backend(holder)
            cutoff = loop.time() + (
                1.5 if mode == "deadline" else CONDITION_DEADLINE_SECONDS
            )
            task = asyncio.create_task(
                wait_on_later_scope(candidate, cutoff, mode, readiness, observed)
            )
            original = await readiness.wait(task)
            if mode != "native":
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
                await settle_scope_contender(task)
        finally:
            if mode == "native":
                event.remove(engine, "handle_error", record_statement_timeout)
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
        assert statement_timeouts == [LATER.key]
    assert committed == [17]
    assert ledger.opened == ledger.closed
    assert ledger.outstanding == 0


@pytest.mark.asyncio
async def test_scope_contender_reports_early_native_refusal(database: Database) -> None:
    async with database.transaction() as writer:
        await writer.exec_driver_sql(CREATE_PROBE)
        await writer.exec_driver_sql(INSERT_PROBE)
    candidate = Database(narrow_settings(database.settings, pool_size=1))
    ledger = ConnectionLedger(candidate)
    readiness = ContenderReadiness()
    deadline = asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS

    task = asyncio.create_task(
        wait_on_later_scope(candidate, deadline, "cancel", readiness, [])
    )
    try:
        with pytest.raises(
            AssertionError,
            match=r"^contender readiness phase=probe_insert outcome=integrity_error$",
        ):
            await readiness.wait(task)
        settled_when_reported = task.done()
    finally:
        try:
            await settle_scope_contender(task)
        finally:
            await candidate.close()
    async with database.read_transaction() as reader:
        values = (await reader.exec_driver_sql(READ_PROBE)).scalars().all()

    assert settled_when_reported
    assert isinstance(task.exception(), IntegrityError)
    assert candidate.units_in_flight() == ledger.outstanding == 0
    assert ledger.checkouts > 0
    assert ledger.opened == ledger.closed > 0
    assert values == [17]


async def check_late_scope_arm(database: Database) -> None:
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

    def hold_timeout_arm(
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
        if "set_config" in statement:
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
                event.listen(sync_connection, "after_cursor_execute", hold_timeout_arm)
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
                        sync_connection, "after_cursor_execute", hold_timeout_arm
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
        assert reads == 1
        assert len(queries) == 2
        assert all("pg_advisory_xact_lock" not in sql for sql, _ in queries)
    else:
        assert reads == 0
        assert queries == []
    assert str(refused.value) == "the scope lock deadline expired"
    assert all(record["event"] != "engine.locks.acquired" for record in records)
    assert "scope_deadline_probe" not in json.dumps(records)
    assert values == []
    assert ledger.outstanding == database.units_in_flight() == 0


async def observe_final_readback_lock(observer: Database, pid: int) -> Backend:
    kind, identity = EARLIER.key
    async with observer.read_transaction() as reader:
        started: object = await reader.scalar(
            text(
                "SELECT activity.backend_start FROM pg_catalog.pg_stat_activity "
                "AS activity JOIN pg_catalog.pg_locks AS lock USING (pid) "
                "WHERE activity.pid = :pid "
                "AND activity.pid <> pg_catalog.pg_backend_pid() "
                "AND activity.xact_start IS NOT NULL AND lock.locktype = 'advisory' "
                "AND lock.granted AND lock.mode = 'ExclusiveLock' "
                "AND lock.objsubid = 2 "
                "AND lock.classid::bigint = :kind AND lock.objid::bigint = :identity"
            ),
            {"pid": pid, "kind": kind & 0xFFFFFFFF, "identity": identity & 0xFFFFFFFF},
        )
    assert isinstance(started, datetime)
    return Backend(pid, started)


async def refuse_final_scope_readback(
    database: Database,
    cutoff: float,
    ready: asyncio.Future[int | None],
    resume_sqlite: asyncio.Event,
    queries: list[tuple[str, tuple[object, ...]]],
    *,
    invalidate: bool,
) -> None:
    async with database.transaction(admission_deadline=cutoff) as writer:
        await writer.exec_driver_sql(INSERT_PROBE)
        locks = TransitionLocks(writer)
        with executed_queries(writer) as submitted:
            try:
                if database.dialect is Dialect.POSTGRESQL:
                    await park_final_postgresql_readback(
                        writer, locks, cutoff, ready, invalidate=invalidate
                    )
                else:
                    ready.set_result(None)
                    await resume_sqlite.wait()
                    try:
                        await locks.acquire_scopes((EARLIER,), deadline=cutoff)
                    except ScopeDeadlineExpired:
                        assert writer.in_transaction()
                        with pytest.raises(LockError, match="cannot be used"):
                            locks.require(None, (EARLIER,))
                        raise
            finally:
                queries.extend(submitted)
        pytest.fail("the expired scope set was handed off")


async def park_final_postgresql_readback(
    writer: AsyncConnection,
    locks: TransitionLocks,
    cutoff: float,
    ready: asyncio.Future[int | None],
    *,
    invalidate: bool,
) -> None:
    raw = await writer.get_raw_connection()
    driver = postgres_driver(raw.driver_connection)
    native = cast(PostgresNativeConnection, driver)
    parked = asyncio.Event()
    reads = 0
    cutoff_cancelled = False

    async def wait_for_cutoff() -> None:
        nonlocal cutoff_cancelled
        try:
            await parked.wait()
        except asyncio.CancelledError:
            cutoff_cancelled = True
            if invalidate:
                raise

    def hold_readback(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        nonlocal reads
        if "FROM pg_catalog.pg_settings" in statement:
            reads += 1
            if reads == 2:
                ready.set_result(driver.get_server_pid())
                await_(wait_for_cutoff())

    sync_connection = writer.sync_connection
    event.listen(sync_connection, "after_cursor_execute", hold_readback)
    try:
        await locks.acquire_scopes((EARLIER,), deadline=cutoff)
    except ScopeDeadlineExpired:
        assert cutoff_cancelled
        assert writer.in_transaction()
        assert writer.invalidated is invalidate
        assert native.is_closed() is invalidate
        if invalidate:
            with pytest.raises(WriterTransactionError, match="not current"):
                locks.require(None, (EARLIER,))
        else:
            assert raw.is_valid
            assert driver.is_in_transaction()
            with pytest.raises(LockError, match="cannot be used"):
                locks.require(None, (EARLIER,))
        raise
    finally:
        event.remove(sync_connection, "after_cursor_execute", hold_readback)


async def check_final_scope_readback(
    database: Database, observer: Database, loop: WriterCutoffLoop, *, invalidate: bool
) -> None:
    postgres = database.dialect is Dialect.POSTGRESQL
    ledger = ConnectionLedger(database)
    ready: asyncio.Future[int | None] = loop.create_future()
    resume_sqlite = asyncio.Event()
    queries: list[tuple[str, tuple[object, ...]]] = []
    cutoff = loop.time() + 2 * CONDITION_DEADLINE_SECONDS
    commits = 0

    def record_commit(connection: Connection) -> None:
        nonlocal commits
        commits += 1

    engine = database.writer_engine.sync_engine
    event.listen(engine, "commit", record_commit)
    original: Backend | None = None
    try:
        with captured(
            "engine",
            LoggingSettings(level=Level.DEBUG),
            "scope-test",
            lambda: datetime.now(UTC),
        ) as capture:
            task = asyncio.create_task(
                refuse_final_scope_readback(
                    database,
                    cutoff,
                    ready,
                    resume_sqlite,
                    queries,
                    invalidate=invalidate,
                )
            )
            try:
                async with asyncio.timeout(CONDITION_DEADLINE_SECONDS):
                    await asyncio.wait(
                        (ready, task), return_when=asyncio.FIRST_COMPLETED
                    )
                    if task.done():
                        await task
                        pytest.fail("the writer ended before the readback cutoff")
                    pid = ready.result()
                    if postgres:
                        assert pid is not None
                        original = await observe_final_readback_lock(observer, pid)
                    else:
                        assert pid is None
                    assert not task.done()
                assert loop.time() < cutoff
                loop.advance_to(cutoff)
                if not postgres:
                    resume_sqlite.set()
                with pytest.raises(ScopeDeadlineExpired) as refused:
                    await asyncio.wait_for(
                        asyncio.shield(task), CONDITION_DEADLINE_SECONDS
                    )
            finally:
                await settle_scope_contender(task)
            records = capture.finish()
    finally:
        event.remove(engine, "commit", record_commit)

    assert str(refused.value) == "the scope lock deadline expired"
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert all(record["event"] != "engine.locks.acquired" for record in records)
    assert "scope_deadline_probe" not in json.dumps(records)
    assert commits == 0
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
    if postgres:
        assert original is not None
        assert len(queries) == 5
        read, arm, acquire, restore, readback = queries
        assert "FROM pg_catalog.pg_settings" in read[0] == readback[0]
        assert "pg_advisory_xact_lock" in acquire[0]
        assert acquire[1] == EARLIER.key
        assert "set_config" in arm[0] == restore[0]
        await verify_settled_postgresql_backend(observer, original, absent=invalidate)
    else:
        assert queries == []

    async with observer.read_transaction() as reader:
        values = (await reader.exec_driver_sql(READ_PROBE)).scalars().all()
    assert values == []
    replacement_cutoff = loop.time() + CONDITION_DEADLINE_SECONDS
    async with database.transaction(
        admission_deadline=replacement_cutoff
    ) as replacement:
        locks = TransitionLocks(replacement)
        await locks.acquire_scopes((EARLIER,), deadline=replacement_cutoff)
        locks.require(None, (EARLIER,))
        await replacement.exec_driver_sql(
            "INSERT INTO scope_deadline_probe VALUES (23)"
        )
    async with observer.read_transaction() as reader:
        committed = (await reader.exec_driver_sql(READ_PROBE)).scalars().all()
    assert committed == [23]
    assert ledger.outstanding == database.units_in_flight() == 0


async def run_late_scope_case(
    settings: DatabaseSettings, loop: WriterCutoffLoop, late_phase: str
) -> None:
    database = Database(narrow_settings(settings, pool_size=1))
    if late_phase == "arm":
        try:
            await check_late_scope_arm(database)
        finally:
            await database.close()
        return

    postgres = database.dialect is Dialect.POSTGRESQL
    observer = (
        Database(narrow_settings(settings, pool_size=1)) if postgres else database
    )
    try:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(CREATE_PROBE)
        await check_final_scope_readback(database, observer, loop, invalidate=False)
        if postgres:
            async with database.transaction() as writer:
                await writer.exec_driver_sql("DELETE FROM scope_deadline_probe")
            await check_final_scope_readback(database, observer, loop, invalidate=True)
    finally:
        try:
            await database.close()
        finally:
            if postgres:
                await observer.close()


@pytest.mark.parametrize("late_phase", ["arm", "readback"])
def test_late_scope_setup_never_hands_off_a_committable_writer(
    database_settings: DatabaseSettings, late_phase: str
) -> None:
    loop = WriterCutoffLoop()

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        runner.run(run_late_scope_case(database_settings, loop, late_phase))
