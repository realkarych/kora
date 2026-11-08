import asyncio
from typing import Literal

import pytest
from sqlalchemy import event
from sqlalchemy.engine import ExecutionContext
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Dialect
from kora.db.tables import storage_address_fence
from kora.db.transactions import WriterTransactionError
from kora.engine.locks import (
    LockError,
    Scope,
    ScopeKind,
    StorageAddressContended,
    StorageAddressMissing,
    TransitionLocks,
)
from tests.db.closing import ConnectionLedger
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.lock_support import DEADLINE, DeliberateRollback, wait_for_lock_waiter

STORE: str = "Store"
DIGEST: str = "sha256:" + "a" * 64
OTHER_DIGEST: str = "sha256:" + "b" * 64


async def seed_address(database: Database) -> None:
    async with database.transaction() as connection:
        await connection.execute(
            storage_address_fence.insert(),
            [
                {
                    "store_id": STORE,
                    "payload_digest": DIGEST,
                    "last_deletion_generation": 0,
                },
                {
                    "store_id": STORE.lower(),
                    "payload_digest": OTHER_DIGEST,
                    "last_deletion_generation": 0,
                },
            ],
        )


async def scoped_locks(connection: AsyncConnection, scope: str) -> TransitionLocks:
    locks = TransitionLocks(connection)
    await locks.acquire_scopes((Scope(ScopeKind.NAMESPACE, (scope,)),))
    return locks


async def foreign_assertion(
    locks: TransitionLocks, connection: AsyncConnection
) -> None:
    locks.require_existing_storage_address(connection, STORE, DIGEST)


@pytest.mark.asyncio
async def test_address_lock_requires_current_writer_and_preceding_scopes(
    database: Database,
) -> None:
    await seed_address(database)

    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        with executed_queries(connection) as premature:
            with pytest.raises(LockError, match="once after scopes"):
                await locks.acquire_existing_storage_address(STORE, DIGEST)
        await locks.acquire_scopes(())
        with executed_queries(connection) as acquired:
            await locks.acquire_existing_storage_address(STORE, DIGEST)
        with executed_queries(connection) as refused:
            locks.require_existing_storage_address(connection, STORE, DIGEST)
            with pytest.raises(LockError, match="once after scopes"):
                await locks.acquire_existing_storage_address(STORE, DIGEST)
            with pytest.raises(LockError, match="not held"):
                locks.require_existing_storage_address(connection, STORE, OTHER_DIGEST)
            with pytest.raises(LockError, match="not held"):
                locks.require_existing_storage_address(
                    connection, STORE.lower(), DIGEST
                )
            with pytest.raises(WriterTransactionError, match="not current"):
                await asyncio.create_task(foreign_assertion(locks, connection))
        if database.dialect is Dialect.POSTGRESQL:
            async with database.transaction() as second_writer:
                second = await scoped_locks(second_writer, "second")
                with pytest.raises(LockError, match="does not own"):
                    locks.require_existing_storage_address(second_writer, STORE, DIGEST)
                second.require_connection(second_writer)
        else:
            async with database.read_transaction() as reader:
                with pytest.raises(LockError, match="does not own"):
                    locks.require_existing_storage_address(reader, STORE, DIGEST)
    with pytest.raises(WriterTransactionError, match="not current"):
        locks.require_existing_storage_address(connection, STORE, DIGEST)

    assert premature == refused == []
    assert len(acquired) == (2 if database.dialect is Dialect.POSTGRESQL else 1)
    assert database.writer_is_free()
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_invalid_address_attempt_issues_no_sql_and_cannot_be_reused(
    database: Database,
) -> None:
    invalid = (
        ("", DIGEST),
        ("x" * 256 + " ", DIGEST),
        ("store\0", DIGEST),
        ("stör", DIGEST),
        (STORE, DIGEST.upper()),
        (STORE, DIGEST + "\n"),
    )

    for store, digest in invalid:
        async with database.transaction() as connection:
            locks = await scoped_locks(connection, "invalid")
            with executed_queries(connection) as queries:
                with pytest.raises(LockError, match="invalid storage address"):
                    await locks.acquire_existing_storage_address(store, digest)
                with pytest.raises(LockError, match="cannot be used"):
                    await locks.acquire_existing_storage_address(STORE, DIGEST)

        assert queries == []


@pytest.mark.asyncio
async def test_existing_address_uses_both_index_keys_and_a_fixed_projection(
    database: Database,
) -> None:
    await seed_address(database)
    async with database.transaction() as connection:
        await connection.execute(
            storage_address_fence.insert(),
            [
                {
                    "store_id": f"history-{index}",
                    "payload_digest": DIGEST,
                    "last_deletion_generation": 0,
                }
                for index in range(1024)
            ],
        )
        await connection.exec_driver_sql("ANALYZE storage_address_fence")
    miss_cases = ((STORE.lower(), DIGEST), (STORE, OTHER_DIGEST))

    async with database.transaction() as connection:
        locks = await scoped_locks(connection, "present")
        with executed_queries(connection) as hit:
            await locks.acquire_existing_storage_address(STORE, DIGEST)
        statement, args = hit[-1]
        projected = (await connection.exec_driver_sql(statement, args)).all()
        prefix = (
            "EXPLAIN QUERY PLAN "
            if database.dialect is Dialect.SQLITE
            else "EXPLAIN (COSTS OFF) "
        )
        hit_plan = (await connection.exec_driver_sql(prefix + statement, args)).all()
    misses: list[list[tuple[object, ...]]] = []
    miss_plans: list[str] = []
    for store, digest in miss_cases:
        async with database.transaction() as connection:
            locks = await scoped_locks(connection, "missing")
            with executed_queries(connection) as miss:
                with pytest.raises(StorageAddressMissing):
                    await locks.acquire_existing_storage_address(store, digest)
                with pytest.raises(LockError, match="cannot be used"):
                    locks.require_existing_storage_address(connection, store, digest)
            statement, args = miss[-1]
            misses.append(
                [
                    tuple(row)
                    for row in await connection.exec_driver_sql(statement, args)
                ]
            )
            miss_plans.append(
                str((await connection.exec_driver_sql(prefix + statement, args)).all())
            )

    assert projected == [(1,)]
    assert misses == [[], []]
    for plan in (str(hit_plan), *miss_plans):
        assert "uq_storage_address_fence_store_id_payload_digest" in plan or (
            database.dialect is Dialect.SQLITE and "sqlite_autoindex_" in plan
        )
        if database.dialect is Dialect.SQLITE:
            assert "store_id=? AND payload_digest=?" in plan
        else:
            assert "Index Cond:" in plan
            assert "store_id =" in plan
            assert "payload_digest =" in plan


@pytest.mark.asyncio
async def test_address_lookup_binds_keys_only_at_execution(database: Database) -> None:
    await seed_address(database)
    engine = database.writer_engine.sync_engine
    defaults: list[dict[str, object]] = []
    inputs: list[tuple[object, ...]] = []
    missing_store = "Missing-Private-Store"

    def observe(
        connection: object,
        cursor: object,
        statement: str,
        parameters: tuple[object, ...],
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        if "FROM storage_address_fence" in statement:
            compiled = context.compiled
            assert compiled is not None
            assert compiled.params is not None
            defaults.append(dict(compiled.params))
            inputs.append(parameters)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        await acquire_address(database, "present", STORE, DIGEST)
        with pytest.raises(StorageAddressMissing):
            await acquire_address(database, "missing", missing_store, OTHER_DIGEST)
    finally:
        event.remove(engine, "before_cursor_execute", observe)

    assert len(defaults) == 2
    assert STORE in inputs[0]
    assert DIGEST in inputs[0]
    assert missing_store in inputs[1]
    assert OTHER_DIGEST in inputs[1]
    for values in defaults:
        for private in (STORE, DIGEST, missing_store, OTHER_DIGEST):
            assert private not in values.values()


async def acquire_address(
    database: Database, scope: str, store: str, digest: str
) -> None:
    async with database.transaction() as connection:
        locks = await scoped_locks(connection, scope)
        await locks.acquire_existing_storage_address(store, digest)
        locks.require_existing_storage_address(connection, store, digest)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["rollback", "cancel"])
async def test_storage_address_native_contention_and_release(
    database: Database, ending: Literal["rollback", "cancel"]
) -> None:
    await seed_address(database)
    ledger = ConnectionLedger(database)
    ready = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with database.transaction() as connection:
            locks = await scoped_locks(connection, "holder")
            await locks.acquire_existing_storage_address(STORE, DIGEST)
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)
            raise DeliberateRollback()

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    follower: asyncio.Task[None] | None = None
    try:
        if database.dialect is Dialect.POSTGRESQL:
            with pytest.raises(StorageAddressContended):
                await asyncio.wait_for(
                    acquire_address(database, "contender", STORE, DIGEST), DEADLINE
                )
            await asyncio.wait_for(
                acquire_address(database, "disjoint", STORE.lower(), OTHER_DIGEST),
                DEADLINE,
            )
        else:
            follower = asyncio.create_task(
                acquire_address(database, "follower", STORE, DIGEST)
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
        acquire_address(database, "after-release", STORE, DIGEST), DEADLINE
    )
    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as connection:
            result = await connection.exec_driver_sql(
                "LOCK TABLE storage_address_fence IN ACCESS EXCLUSIVE MODE NOWAIT"
            )
            result.close()

    assert ledger.outstanding == 0
    assert database.writer_is_free()
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_existing_address_table_contention_refuses_before_select(
    database: Database,
) -> None:
    await seed_address(database)

    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as blocker:
            blocked = await blocker.exec_driver_sql(
                "LOCK TABLE storage_address_fence IN ACCESS EXCLUSIVE MODE NOWAIT"
            )
            blocked.close()
            async with database.transaction() as contender:
                locks = await scoped_locks(contender, "table-contender")
                with executed_queries(contender) as statements:
                    async with asyncio.timeout(DEADLINE):
                        with pytest.raises(StorageAddressContended):
                            await locks.acquire_existing_storage_address(STORE, DIGEST)
                await contender.rollback()
        assert statements == [
            ("LOCK TABLE storage_address_fence IN ROW SHARE MODE NOWAIT", ())
        ]
    await acquire_address(database, "table-released", STORE, DIGEST)

    assert database.writer_is_free()
    assert database.units_in_flight() == 0
