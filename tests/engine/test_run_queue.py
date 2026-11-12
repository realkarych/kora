import asyncio
from dataclasses import dataclass
from traceback import walk_tb
from typing import Literal

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.conformance.capture import captured
from kora.db.connection import Database, Dialect
from kora.db.run_queue_index_v1 import (
    POSTGRES_QUEUE_INDEX,
    SQLITE_QUEUE_INDEX,
)
from kora.db.tables import run
from kora.db.transactions import managed_writer
from kora.engine.locks import COMPONENT, Scope, ScopeKind, TransitionLocks
from kora.engine.run_queue import (
    POSTGRES_QUEUE_PROBE,
    QueueCheckFailed,
    QueueCheckFailure,
    QueueState,
    observe_run_queue,
)
from kora.engine.writer_mode import WRITABLE_POSTGRES_MODE
from kora.observability import Level, LoggingSettings
from tests.db.closing import ConnectionLedger
from tests.db.seed import FakeClock, Seeder
from tests.db.test_graph_read_admission import executed_queries, sqlite_instructions
from tests.engine.lock_support import (
    DEADLINE,
    lock_inventory,
    release_and_join,
    wait_for_lock_waiter,
)
from tests.engine.test_start_replay import observed_statements

INVALID_RUN = "private-invalid-run-key"


@dataclass(frozen=True)
class QueueCase:
    namespace: str
    graph: str
    candidate: str
    oldest: str
    earlier: str
    later: str

    @property
    def scopes(self) -> tuple[Scope, ...]:
        return (
            Scope(ScopeKind.NAMESPACE, (self.namespace,)),
            Scope(ScopeKind.GRAPH, (self.graph,)),
        )

    async def observe(
        self, connection: AsyncConnection, locks: TransitionLocks
    ) -> QueueState:
        return await observe_run_queue(
            connection, locks, self.namespace, self.graph, self.candidate
        )


async def queue_case(database: Database) -> QueueCase:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace = await seed.namespace()
        other_namespace = await seed.namespace("other")
        graph = await seed.graph(namespace)
        other_graph = await seed.graph(other_namespace)
        old = await seed.graph_version(graph)
        current = await seed.graph_version(graph, 2)
        foreign = await seed.graph_version(other_graph)
        await seed.run(other_namespace, other_graph, foreign, "running")
        await seed.run(other_namespace, graph, old, "running")
        oldest = await seed.run(namespace, graph, old, "pending")
        earlier = await seed.run(namespace, graph, old, "pending")
        candidate = await seed.run(namespace, graph, current, "pending")
        later = await seed.run(namespace, graph, current, "pending")
    return QueueCase(namespace, graph, candidate, oldest, earlier, later)


async def acquire(writer: AsyncConnection, case: QueueCase) -> TransitionLocks:
    locks = TransitionLocks(writer)
    await locks.acquire_run(case.candidate)
    await locks.acquire_scopes(case.scopes)
    return locks


@pytest.mark.asyncio
async def test_queue_precedence_and_exact_scope_across_versions(
    database: Database,
) -> None:
    case = await queue_case(database)
    ledger = ConnectionLedger(database)
    states: list[QueueState] = []
    marker = {"private": INVALID_RUN}

    with captured(
        COMPONENT, LoggingSettings(level=Level.DEBUG), "queue-test", FakeClock().now
    ) as capture:
        async with database.transaction() as writer:
            locks = await acquire(writer, case)
            checked_out = ledger.checkouts
            states.append(await case.observe(writer, locks))
            await writer.execute(
                update(run)
                .where(run.c.id == case.later)
                .values(status="running", params=marker)
            )
            states.append(await case.observe(writer, locks))
            await writer.execute(
                update(run).where(run.c.id == case.earlier).values(id=INVALID_RUN)
            )
            states.append(await case.observe(writer, locks))
            await writer.execute(
                update(run).where(run.c.id == INVALID_RUN).values(status="running")
            )
            await writer.execute(
                update(run).where(run.c.id == case.later).values(status="succeeded")
            )
            states.append(await case.observe(writer, locks))
            await writer.execute(
                update(run)
                .where(run.c.id.in_((INVALID_RUN, case.oldest)))
                .values(status="succeeded")
            )
            states.append(await case.observe(writer, locks))
            assert ledger.checkouts == checked_out
        records = capture.finish()

    assert states == [
        QueueState.OLDER_PENDING,
        QueueState.RUNNING,
        QueueState.INVALID_ORDER,
        QueueState.RUNNING,
        QueueState.CLEAR,
    ]
    assert [
        entry["outcome"]
        for entry in records
        if entry["event"] == "engine.run_queue.checked"
    ] == [state.value for state in states]
    assert all(
        secret not in repr(records)
        for secret in (INVALID_RUN, case.namespace, case.graph, case.candidate)
    )
    assert database.units_in_flight() == ledger.outstanding == 0


@pytest.mark.asyncio
async def test_queue_checks_fresh_state_after_waiting_for_graph_lock(
    database: Database,
) -> None:
    case = await queue_case(database)
    ready, release = asyncio.Event(), asyncio.Event()
    states: list[QueueState] = []
    ledger = ConnectionLedger(database)

    async def predecessor() -> None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(case.earlier)
            await locks.acquire_scopes(case.scopes)
            await writer.execute(
                update(run).where(run.c.id == case.earlier).values(status="running")
            )
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)

    async def successor() -> None:
        async with database.transaction() as writer:
            locks = await acquire(writer, case)
            states.append(await case.observe(writer, locks))

    owner = asyncio.create_task(predecessor())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    contender = asyncio.create_task(successor())
    try:
        await wait_for_lock_waiter(database)
    finally:
        await release_and_join(release, owner, contender)

    assert states == [QueueState.RUNNING]
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_queue_refuses_bad_identity_or_missing_exact_locks_without_sql(
    database: Database,
) -> None:
    case = await queue_case(database)
    ledger = ConnectionLedger(database)
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(case.later)
        await locks.acquire_scopes(case.scopes)

        async with observed_statements(writer) as queries:
            with pytest.raises(QueueCheckFailed) as missing:
                await case.observe(writer, locks)
            with pytest.raises(QueueCheckFailed) as identity:
                await observe_run_queue(
                    writer, locks, INVALID_RUN, case.graph, case.candidate
                )

        assert queries == []
        assert missing.value.reason is QueueCheckFailure.LOCKS
        assert identity.value.reason is QueueCheckFailure.IDENTITY
        assert identity.value.__cause__ is identity.value.__context__ is None
        assert [
            set(frame.f_locals)
            for frame, _ in walk_tb(identity.value.__traceback__)
            if frame.f_code.co_name == "observe_run_queue"
        ] == [{"state"}]
        async with database.read_transaction() as foreign:
            async with observed_statements(foreign) as foreign_queries:
                with pytest.raises(QueueCheckFailed) as ownership:
                    await case.observe(foreign, locks)
        assert foreign_queries == []
        assert ownership.value.reason is QueueCheckFailure.OWNERSHIP
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["readonly", "repeatable", "autocommit"])
async def test_queue_refuses_unsupported_postgres_writer_mode(
    database: Database, mode: Literal["readonly", "repeatable", "autocommit"]
) -> None:
    case = await queue_case(database)
    ledger = ConnectionLedger(database)

    async def refuse(writer: AsyncConnection) -> None:
        locks = await acquire(writer, case)
        async with observed_statements(writer) as queries:
            with pytest.raises(QueueCheckFailed) as refusal:
                await case.observe(writer, locks)
        assert refusal.value.reason is QueueCheckFailure.MODE
        assert queries == (
            [] if mode == "autocommit" else [WRITABLE_POSTGRES_MODE.text]
        )

    if database.dialect is Dialect.POSTGRESQL:
        if mode == "autocommit":
            async with database.writer_engine.connect() as writer:
                await writer.execution_options(isolation_level="AUTOCOMMIT")
                async with writer.begin():
                    with managed_writer(writer):
                        await refuse(writer)
        else:
            async with database.transaction() as writer:
                await writer.execute(
                    text(
                        "SET TRANSACTION READ ONLY"
                        if mode == "readonly"
                        else "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                    )
                )
                await refuse(writer)
    async with database.transaction() as writer:
        result = await case.observe(writer, await acquire(writer, case))

    assert result is QueueState.OLDER_PENDING
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0


async def sqlite_plan(writer: AsyncConnection, case: QueueCase) -> None:
    locks = await acquire(writer, case)
    with executed_queries(writer) as queries:
        result = await case.observe(writer, locks)
    [(statement, bindings)] = queries
    program = await sqlite_instructions(writer, statement, bindings)
    run_root = await writer.scalar(
        text("SELECT rootpage FROM main.sqlite_schema WHERE name = 'run'")
    )
    index_root = await writer.scalar(
        text("SELECT rootpage FROM main.sqlite_schema WHERE name = :index"),
        {"index": SQLITE_QUEUE_INDEX},
    )
    run_cursors = {
        row[2] for row in program if row[1] == "OpenRead" and row[3] == run_root
    }
    index_cursors = {
        row[2] for row in program if row[1] == "OpenRead" and row[3] == index_root
    }

    assert result is QueueState.CLEAR
    assert len(index_cursors) == 3
    assert sorted(
        row[5]
        for row in program
        if row[2] in index_cursors
        and row[1] in {"SeekGE", "SeekGT"}
        and type(row[5]) is str
    ) == ["3", "4", "4"]
    assert all(
        row[2] not in run_cursors
        for row in program
        if row[1] in {"Column", "RowData", "Rowid", "SeekRowid", "NotExists"}
    )
    assert not any(row[1] in {"Rewind", "Last", "IdxRowid"} for row in program)


async def postgres_plan(writer: AsyncConnection, case: QueueCase) -> None:
    statement = POSTGRES_QUEUE_PROBE.text
    for index, field in enumerate(("namespace_id", "graph_id", "run_id"), start=1):
        statement = statement.replace(f":{field}", f"${index}")
    values = ", ".join(
        f"'{value}'" for value in (case.namespace, case.graph, case.candidate)
    )
    await writer.execute(text("ANALYZE run"))
    await writer.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    await writer.exec_driver_sql(
        f"PREPARE run_queue_test(varchar, varchar, varchar) AS {statement}"
    )
    try:
        with await writer.exec_driver_sql(
            f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) EXECUTE run_queue_test({values})"
        ) as result:
            lines = [str(row[0]) for row in result]
    finally:
        await writer.execute(text("DEALLOCATE run_queue_test"))
    paths = [line for line in lines if "Scan" in line]
    conditions = [line for line in lines if "Index Cond:" in line]

    assert len(paths) == len(conditions) == 3
    assert all(f"using {POSTGRES_QUEUE_INDEX}" in path for path in paths)
    assert all(
        all(field in line for field in ("namespace_id", "graph_id", "status"))
        for line in conditions
    )
    key_conditions = [
        line.rsplit("END", 1)[-1]
        .replace("(", "")
        .replace(")", "")
        .replace("::text", "")
        .strip()
        for line in conditions
        if "'pending'" in line and "CASE" in line
    ]
    assert len(key_conditions) == 2
    assert any(value.startswith("= ''") for value in key_conditions), conditions
    assert any(value.startswith("< $3") for value in key_conditions), conditions


@pytest.mark.asyncio
async def test_native_queue_plan_excludes_terminal_run_payloads(
    database: Database,
) -> None:
    case = await queue_case(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        version = await seed.graph_version(case.graph, 3)
        history = [
            seed.run_values(case.namespace, case.graph, version, "succeeded", False)
            for _ in range(4096)
        ]
        await writer.execute(insert(run), history)
        await writer.execute(
            update(run)
            .where(run.c.id.in_((case.oldest, case.earlier)))
            .values(status="succeeded")
        )

        if database.dialect is Dialect.SQLITE:
            await sqlite_plan(writer, case)
        else:
            await postgres_plan(writer, case)

    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_queue_uses_owned_schema_without_rechecking_indexes(
    database: Database,
) -> None:
    case = await queue_case(database)

    async with database.transaction() as writer:
        locks = await acquire(writer, case)
        async with observed_statements(writer) as queries:
            outcome = await case.observe(writer, locks)

    assert outcome is QueueState.OLDER_PENDING
    assert not any("pg_index" in query or "sqlite_schema" in query for query in queries)
    assert database.units_in_flight() == 0
