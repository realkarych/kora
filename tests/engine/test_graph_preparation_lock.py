import asyncio
from dataclasses import replace
from typing import Literal

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import ExecutionContext
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Dialect
from kora.db.graph_preparation_reads import (
    GraphPreparationBinding,
    GraphPreparationSettlement,
    GraphPreparationState,
    read_graph_preparation_settlement,
)
from kora.db.tables import graph_deployment_preparation
from kora.db.transactions import WriterTransactionError
from kora.engine.locks import (
    GraphPreparationLockContended,
    GraphPreparationLockRejected,
    LockError,
    TransitionLocks,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_preparation_reads import seed_preparation
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.lock_support import DEADLINE, DeliberateRollback, wait_for_lock_waiter
from tests.engine.test_storage_address_lock import (
    DIGEST,
    OTHER_DIGEST,
    STORE,
    scoped_locks,
    seed_address,
)


async def stage_locks(
    connection: AsyncConnection, scope: str, store: str = STORE, digest: str = DIGEST
) -> TransitionLocks:
    locks = await scoped_locks(connection, scope)
    await locks.acquire_existing_storage_address(store, digest)
    return locks


async def lock_preparation(
    database: Database,
    scope: str,
    binding: GraphPreparationBinding,
    store: str = STORE,
    digest: str = DIGEST,
) -> None:
    async with database.transaction() as connection:
        locks = await stage_locks(connection, scope, store, digest)
        await locks.acquire_open_graph_preparation(connection, store, digest, binding)
        locks.require_open_graph_preparation(connection, store, digest, binding)


async def foreign_assertion(
    locks: TransitionLocks,
    connection: AsyncConnection,
    binding: GraphPreparationBinding,
) -> None:
    locks.require_open_graph_preparation(connection, STORE, DIGEST, binding)


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_open_preparation_guard_binds_current_writer_after_address(
    database: Database, bound: bool
) -> None:
    await seed_address(database)
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "open", bound=bound)

    async with database.transaction() as connection:
        locks = await scoped_locks(connection, "selected")
        with executed_queries(connection) as early:
            with pytest.raises(LockError, match="address lock is not held"):
                await locks.acquire_open_graph_preparation(
                    connection, STORE, DIGEST, binding
                )
        await locks.acquire_existing_storage_address(STORE, DIGEST)
        with executed_queries(connection) as assertions:
            with pytest.raises(LockError, match="address lock is not held"):
                await locks.acquire_open_graph_preparation(
                    connection, STORE, OTHER_DIGEST, binding
                )
        await locks.acquire_open_graph_preparation(connection, STORE, DIGEST, binding)
        with executed_queries(connection) as after:
            locks.require_open_graph_preparation(connection, STORE, DIGEST, binding)
            with pytest.raises(LockError, match="acquired once"):
                await locks.acquire_open_graph_preparation(
                    connection, STORE, DIGEST, binding
                )
            with pytest.raises(LockError, match="not held"):
                locks.require_open_graph_preparation(
                    connection, STORE, DIGEST, replace(binding, graph_name="different")
                )
            with pytest.raises(WriterTransactionError, match="not current"):
                await asyncio.create_task(foreign_assertion(locks, connection, binding))
        if database.dialect is Dialect.POSTGRESQL:
            async with database.transaction() as other:
                second = await stage_locks(other, "second", STORE.lower(), OTHER_DIGEST)
                with pytest.raises(LockError, match="does not own"):
                    locks.require_open_graph_preparation(other, STORE, DIGEST, binding)
                second.require_connection(other)
    with pytest.raises(WriterTransactionError, match="not current"):
        locks.require_open_graph_preparation(connection, STORE, DIGEST, binding)

    assert early == assertions == after == []
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["closed_without_publication", "published", "reused"])
async def test_preparation_guard_refuses_each_terminal_state(
    database: Database, state: GraphPreparationState
) -> None:
    await seed_address(database)
    async with database.transaction() as connection:
        binding, expected = await seed_preparation(connection, state, bound=True)

    with pytest.raises(GraphPreparationLockRejected):
        await lock_preparation(database, "selected", binding)
    async with database.read_snapshot() as connection:
        actual = await read_graph_preparation_settlement(connection, binding)

    assert actual == expected


@pytest.mark.asyncio
async def test_preparation_guard_rejects_mismatches_and_consumes_invalid_attempt(
    database: Database,
) -> None:
    await seed_address(database)
    async with database.transaction() as connection:
        bound, _ = await seed_preparation(connection, "open", bound=True)
        unbound, _ = await seed_preparation(
            connection, "open", bound=False, namespace_name="other"
        )
        missing = Seeder(connection).new_id()
    wrong = (
        replace(bound, preparation_id=missing),
        replace(bound, namespace_id=unbound.namespace_id),
        replace(bound, graph_name="different"),
        replace(bound, initial_graph_id=None),
        replace(unbound, initial_graph_id=bound.initial_graph_id),
        replace(bound, deployment_digest="b" * 64),
    )

    for candidate in wrong:
        with pytest.raises(GraphPreparationLockRejected):
            await lock_preparation(database, "wrong", candidate)
    async with database.transaction() as connection:
        locks = await stage_locks(connection, "invalid")
        with executed_queries(connection) as queries:
            with pytest.raises(GraphPreparationLockRejected):
                await locks.acquire_open_graph_preparation(
                    connection,
                    STORE,
                    DIGEST,
                    replace(bound, graph_name="malformed-name"),
                )
            with pytest.raises(LockError, match="cannot be used"):
                await locks.acquire_open_graph_preparation(
                    connection, STORE, DIGEST, bound
                )

    assert queries == []


@pytest.mark.asyncio
async def test_preparation_guard_and_reader_use_exact_index_and_execute_time_binds(
    database: Database,
) -> None:
    await seed_address(database)
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "open", bound=True)
        seeder = Seeder(connection)
        missing = seeder.new_id()
        await connection.execute(
            graph_deployment_preparation.insert(),
            [
                {
                    "id": seeder.new_id(),
                    "namespace_id": binding.namespace_id,
                    "graph_name": binding.graph_name,
                    "deployment_digest": binding.deployment_digest,
                    "state": "open",
                }
                for _ in range(1024)
            ],
        )
        await connection.exec_driver_sql("ANALYZE graph_deployment_preparation")
    prefixes = (
        "EXPLAIN QUERY PLAN "
        if database.dialect is Dialect.SQLITE
        else "EXPLAIN (COSTS OFF) "
    )
    defaults: list[dict[str, object]] = []
    parameters: list[tuple[object, ...]] = []
    engines = {
        database.writer_engine.sync_engine,
        database.reader_engine.sync_engine,
    }

    def observe(
        connection: object,
        cursor: object,
        statement: str,
        values: tuple[object, ...],
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        if (
            "FROM graph_deployment_preparation" in statement
            and context.compiled is not None
        ):
            assert context.compiled.params is not None
            defaults.append(dict(context.compiled.params))
            parameters.append(values)

    for engine in engines:
        event.listen(engine, "before_cursor_execute", observe)
    plans: list[str] = []
    try:
        async with database.read_snapshot() as reader:
            with executed_queries(reader) as reads:
                actual = await read_graph_preparation_settlement(reader, binding)
        for candidate in (
            binding,
            replace(binding, preparation_id=missing),
            replace(binding, graph_name="different"),
        ):
            async with database.transaction() as writer:
                locks = await stage_locks(writer, "selected")
                with executed_queries(writer) as writes:
                    if candidate is binding:
                        await locks.acquire_open_graph_preparation(
                            writer, STORE, DIGEST, candidate
                        )
                    else:
                        with pytest.raises(GraphPreparationLockRejected):
                            await locks.acquire_open_graph_preparation(
                                writer, STORE, DIGEST, candidate
                            )
                statement, values = writes[-1]
                plans.append(
                    str(
                        (
                            await writer.exec_driver_sql(prefixes + statement, values)
                        ).all()
                    )
                )
                assert ("FOR UPDATE NOWAIT" in statement) == (
                    database.dialect is Dialect.POSTGRESQL
                )
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", observe)

    assert actual == GraphPreparationSettlement("open", None, None)
    [(read_statement, _)] = reads
    assert "FOR UPDATE" not in read_statement
    assert len(defaults) == 4
    private = (
        binding.preparation_id,
        binding.namespace_id,
        binding.graph_name,
        binding.initial_graph_id,
        binding.deployment_digest,
    )
    assert all(value in parameters[0] and value in parameters[1] for value in private)
    assert all(
        value not in compiled.values()
        for value in (*private, missing)
        for compiled in defaults
    )
    for plan in plans:
        if database.dialect is Dialect.SQLITE:
            assert "SEARCH graph_deployment_preparation" in plan
            assert "id=?" in plan
        else:
            assert "Index Cond:" in plan
            assert "id =" in plan
            assert "pk_graph_deployment_preparation" in plan


@pytest.mark.asyncio
async def test_preparation_guard_masks_malformed_native_witness(
    database: Database,
) -> None:
    await seed_address(database)
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "open", bound=False)
        sqlite = database.dialect is Dialect.SQLITE
        if sqlite:
            await connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        else:
            await connection.exec_driver_sql(
                "ALTER TABLE graph_deployment_preparation "
                "DROP CONSTRAINT ck_graph_deployment_preparation_result_shape, "
                "DROP CONSTRAINT ck_graph_deployment_preparation_result_target, "
                "DROP CONSTRAINT ck_graph_deployment_preparation_result_graph_id_domain"
            )
        try:
            await connection.execute(
                graph_deployment_preparation.update().values(result_graph_id="x" * 8192)
            )
        finally:
            if sqlite:
                await connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")

    async with database.transaction() as connection:
        locks = await stage_locks(connection, "selected")
        with executed_queries(connection) as queries:
            with pytest.raises(GraphPreparationLockRejected):
                await locks.acquire_open_graph_preparation(
                    connection, STORE, DIGEST, binding
                )
        statement, parameters = queries[-1]
        projected = (await connection.exec_driver_sql(statement, parameters)).one()

    assert tuple(projected) == (None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["rollback", "cancel"])
async def test_preparation_guard_serializes_native_row_and_releases_writer(
    database: Database, ending: Literal["rollback", "cancel"]
) -> None:
    await seed_address(database)
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "open", bound=True)
        disjoint, _ = await seed_preparation(
            connection, "open", bound=False, namespace_name="disjoint"
        )
    ledger = ConnectionLedger(database)
    ready = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with database.transaction() as connection:
            locks = await stage_locks(connection, "holder")
            await locks.acquire_open_graph_preparation(
                connection, STORE, DIGEST, binding
            )
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)
            raise DeliberateRollback()

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    follower: asyncio.Task[None] | None = None
    try:
        if database.dialect is Dialect.POSTGRESQL:
            with pytest.raises(GraphPreparationLockContended):
                await asyncio.wait_for(
                    lock_preparation(
                        database, "contender", binding, STORE.lower(), OTHER_DIGEST
                    ),
                    DEADLINE,
                )
            await asyncio.wait_for(
                lock_preparation(
                    database, "disjoint", disjoint, STORE.lower(), OTHER_DIGEST
                ),
                DEADLINE,
            )
        else:
            follower = asyncio.create_task(
                lock_preparation(database, "follower", binding)
            )
            await wait_for_lock_waiter(database)
            assert not follower.done()
    finally:
        if ending == "cancel":
            holder.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(holder, DEADLINE)
        else:
            release.set()
            with pytest.raises(DeliberateRollback):
                await asyncio.wait_for(holder, DEADLINE)
        if follower is not None:
            await asyncio.wait_for(follower, DEADLINE)
    await asyncio.wait_for(
        lock_preparation(database, "after-release", binding), DEADLINE
    )

    assert ledger.outstanding == 0
    assert database.writer_is_free()
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_preparation_guard_table_conflict(
    database: Database,
) -> None:
    await seed_address(database)
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "open", bound=False)

    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as blocker:
            result = await blocker.exec_driver_sql(
                "LOCK TABLE graph_deployment_preparation "
                "IN ACCESS EXCLUSIVE MODE NOWAIT"
            )
            result.close()
            async with database.transaction() as contender:
                locks = await stage_locks(contender, "table-contender")
                with executed_queries(contender) as statements:
                    async with asyncio.timeout(DEADLINE):
                        with pytest.raises(GraphPreparationLockContended):
                            await locks.acquire_open_graph_preparation(
                                contender, STORE, DIGEST, binding
                            )
                await contender.rollback()
        assert statements == [
            ("LOCK TABLE graph_deployment_preparation IN ROW SHARE MODE NOWAIT", ())
        ]
    await lock_preparation(database, "after-table-check", binding)

    assert database.units_in_flight() == 0


async def close_preparation(
    connection: AsyncConnection, binding: GraphPreparationBinding
) -> None:
    await connection.execute(
        graph_deployment_preparation.update()
        .where(graph_deployment_preparation.c.id == binding.preparation_id)
        .values(state="closed_without_publication")
    )


async def wait_for_native_settler(database: Database, backend_id: int) -> None:
    async with database.read_transaction() as observer, asyncio.timeout(DEADLINE):
        while not await observer.scalar(
            text("SELECT cardinality(pg_blocking_pids(:backend_id)) > 0"),
            {"backend_id": backend_id},
        ):
            pass


@pytest.mark.asyncio
async def test_preparation_guard_commits_before_a_waiting_settlement(
    database: Database,
) -> None:
    await seed_address(database)
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "open", bound=False)
    backend_id: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def settle() -> None:
        async with database.transaction() as connection:
            if database.dialect is Dialect.POSTGRESQL:
                pid: object = await connection.scalar(text("SELECT pg_backend_pid()"))
                assert isinstance(pid, int)
                backend_id.set_result(pid)
            await close_preparation(connection, binding)

    settler: asyncio.Task[None] | None = None
    try:
        async with database.transaction() as connection:
            locks = await stage_locks(connection, "before-settlement")
            await locks.acquire_open_graph_preparation(
                connection, STORE, DIGEST, binding
            )
            settler = asyncio.create_task(settle())
            if database.dialect is Dialect.POSTGRESQL:
                await wait_for_native_settler(
                    database, await asyncio.wait_for(backend_id, DEADLINE)
                )
            else:
                await wait_for_lock_waiter(database)
            assert not settler.done()
            locks.require_open_graph_preparation(connection, STORE, DIGEST, binding)
    finally:
        if settler is not None:
            await asyncio.wait_for(settler, DEADLINE)
    with pytest.raises(GraphPreparationLockRejected):
        await lock_preparation(database, "after-settlement", binding)

    assert database.writer_is_free()
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_preparation_guard_refuses_a_settlement_committed_first(
    database: Database,
) -> None:
    await seed_address(database)
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "open", bound=False)
    ready = asyncio.Event()
    release = asyncio.Event()

    async def settle() -> None:
        async with database.transaction() as connection:
            await close_preparation(connection, binding)
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)

    settler = asyncio.create_task(settle())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    follower: asyncio.Task[None] | None = None
    try:
        if database.dialect is Dialect.POSTGRESQL:
            with pytest.raises(GraphPreparationLockContended):
                await asyncio.wait_for(
                    lock_preparation(database, "before-commit", binding), DEADLINE
                )
        else:
            follower = asyncio.create_task(
                lock_preparation(database, "while-settling", binding)
            )
            await wait_for_lock_waiter(database)
            assert not follower.done()
    finally:
        release.set()
        await asyncio.wait_for(settler, DEADLINE)
        if follower is not None:
            with pytest.raises(GraphPreparationLockRejected):
                await asyncio.wait_for(follower, DEADLINE)
    with pytest.raises(GraphPreparationLockRejected):
        await asyncio.wait_for(
            lock_preparation(database, "after-commit", binding), DEADLINE
        )

    assert database.writer_is_free()
    assert database.units_in_flight() == 0
