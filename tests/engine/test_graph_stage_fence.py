import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import ExecutionContext
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.db.connection import Database, Dialect
from kora.db.graph_preparation_reads import GraphPreparationBinding
from kora.db.tables import storage_address_fence
from kora.db.transactions import WriterTransactionError
from kora.engine.locks import LockError, StorageAddressStageUnavailable, TransitionLocks
from kora.protocols.identity_records import MAX_COUNT
from tests.db.closing import ConnectionLedger
from tests.db.test_graph_preparation_reads import seed_preparation
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.lock_support import DEADLINE
from tests.engine.test_graph_preparation_lock import stage_locks
from tests.engine.test_storage_address_lock import (
    DIGEST,
    OTHER_DIGEST,
    STORE,
    seed_address,
)


async def prepare_fence(database: Database) -> GraphPreparationBinding:
    await seed_address(database)
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "open", bound=False)
    return binding


async def lock_stage(
    connection: AsyncConnection, binding: GraphPreparationBinding
) -> TransitionLocks:
    locks = await stage_locks(connection, "stage")
    await locks.acquire_open_graph_preparation(connection, STORE, DIGEST, binding)
    return locks


def address_statement_key(statement: str) -> bool:
    return "FROM storage_address_fence" in statement and "CASE WHEN" in statement


@pytest.mark.asyncio
async def test_stage_fence_requires_both_guards_and_the_exact_live_writer(
    database: Database,
) -> None:
    binding = await prepare_fence(database)
    async with database.transaction() as connection:
        locks = await stage_locks(connection, "stage")
        with executed_queries(connection) as early:
            with pytest.raises(LockError, match="preparation lock is not held"):
                await locks.read_stage_fence_incarnation(
                    connection, STORE, DIGEST, binding
                )
        await locks.acquire_open_graph_preparation(connection, STORE, DIGEST, binding)

        with executed_queries(connection) as wrong:
            with pytest.raises(LockError, match="address lock is not held"):
                await locks.read_stage_fence_incarnation(
                    connection, STORE, OTHER_DIGEST, binding
                )
            with pytest.raises(LockError, match="preparation lock is not held"):
                await locks.read_stage_fence_incarnation(
                    connection, STORE, DIGEST, replace(binding, graph_name="different")
                )
            with pytest.raises(WriterTransactionError, match="not current"):
                await asyncio.create_task(
                    locks.read_stage_fence_incarnation(
                        connection, STORE, DIGEST, binding
                    )
                )
            async with database.read_transaction() as reader:
                with pytest.raises(LockError, match="does not own"):
                    await locks.read_stage_fence_incarnation(
                        reader, STORE, DIGEST, binding
                    )
        incarnation = await locks.read_stage_fence_incarnation(
            connection, STORE, DIGEST, binding
        )
    with pytest.raises(WriterTransactionError, match="not current"):
        await locks.read_stage_fence_incarnation(connection, STORE, DIGEST, binding)

    assert early == wrong == []
    assert incarnation == 1
    assert database.units_in_flight() == 0


async def set_generation(
    connection: AsyncConnection, last: int, pending: int | None
) -> None:
    await connection.execute(
        storage_address_fence.update()
        .where(storage_address_fence.c.store_id == STORE)
        .values(last_deletion_generation=last, pending_deletion_generation=pending)
    )


@pytest.mark.asyncio
async def test_stage_fence_reads_fresh_generation_and_keeps_settlement_access(
    database: Database,
) -> None:
    binding = await prepare_fence(database)
    async with database.transaction() as connection:
        if database.dialect is Dialect.POSTGRESQL:
            await connection.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence('storage_address_fence', "
                    "'fence_incarnation'), :previous)"
                ),
                {"previous": MAX_COUNT - 1},
            )
        else:
            await connection.execute(
                text(
                    "UPDATE sqlite_sequence SET seq = :previous "
                    "WHERE name = 'storage_address_fence'"
                ),
                {"previous": MAX_COUNT - 1},
            )
        await connection.execute(
            storage_address_fence.delete().where(
                storage_address_fence.c.store_id == STORE
            )
        )
        await connection.execute(
            storage_address_fence.insert().values(
                store_id=STORE, payload_digest=DIGEST, last_deletion_generation=0
            )
        )

    async with database.transaction() as connection:
        locks = await lock_stage(connection, binding)
        before = await locks.read_stage_fence_incarnation(
            connection, STORE, DIGEST, binding
        )
        await set_generation(connection, 7, 7)
        with pytest.raises(StorageAddressStageUnavailable):
            await locks.read_stage_fence_incarnation(connection, STORE, DIGEST, binding)
        locks.require_existing_storage_address(connection, STORE, DIGEST)
        await set_generation(connection, MAX_COUNT - 1, None)
        cleared = await locks.read_stage_fence_incarnation(
            connection, STORE, DIGEST, binding
        )
        await set_generation(connection, MAX_COUNT, None)
        with pytest.raises(StorageAddressStageUnavailable):
            await locks.read_stage_fence_incarnation(connection, STORE, DIGEST, binding)
        locks.require_existing_storage_address(connection, STORE, DIGEST)

    assert before == cleared == MAX_COUNT
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_stage_fence_uses_new_incarnation_after_recreation(
    database: Database,
) -> None:
    binding = await prepare_fence(database)
    ledger = ConnectionLedger(database)
    async with database.transaction() as old:
        old_locks = await lock_stage(old, binding)
        initial = await old_locks.read_stage_fence_incarnation(
            old, STORE, DIGEST, binding
        )

    async with database.transaction() as connection:
        await connection.execute(
            storage_address_fence.delete().where(
                storage_address_fence.c.store_id == STORE
            )
        )
        await connection.execute(
            storage_address_fence.insert().values(
                store_id=STORE, payload_digest=DIGEST, last_deletion_generation=0
            )
        )
    async with database.transaction() as current:
        new_locks = await lock_stage(current, binding)
        replacement = await new_locks.read_stage_fence_incarnation(
            current, STORE, DIGEST, binding
        )
        with pytest.raises(WriterTransactionError, match="not current"):
            await old_locks.read_stage_fence_incarnation(
                current, STORE, DIGEST, binding
            )

    assert 0 < initial < replacement
    assert ledger.outstanding == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_stage_fence_refuses_native_malformed_values_before_transport(
    database: Database,
) -> None:
    binding = await prepare_fence(database)
    invalid: tuple[tuple[str, float | str | bytes | int], ...] = (
        (("real", 0.5), ("text", "x" * 8192), ("blob", b"x" * 8192))
        if database.dialect is Dialect.SQLITE
        else (("bigint", -1),)
    )
    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as connection:
            await connection.exec_driver_sql(
                "ALTER TABLE storage_address_fence DROP CONSTRAINT "
                "ck_storage_address_fence_last_generation_nonnegative"
            )

    projected: list[tuple[object, ...]] = []
    async with database.transaction() as connection:
        locks = await lock_stage(connection, binding)
        for native_type, value in invalid:
            if database.dialect is Dialect.SQLITE:
                await connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
                try:
                    await connection.exec_driver_sql(
                        "UPDATE storage_address_fence SET last_deletion_generation=? "
                        "WHERE store_id=? AND payload_digest=?",
                        (value, STORE, DIGEST),
                    )
                finally:
                    await connection.exec_driver_sql(
                        "PRAGMA ignore_check_constraints=OFF"
                    )
                assert (
                    await connection.scalar(
                        text(
                            "SELECT typeof(last_deletion_generation) "
                            "FROM storage_address_fence WHERE store_id = :store"
                        ),
                        {"store": STORE},
                    )
                    == native_type
                )
            else:
                assert isinstance(value, int)
                await set_generation(connection, value, None)
            with executed_queries(connection) as reads:
                with pytest.raises(StorageAddressStageUnavailable):
                    await locks.read_stage_fence_incarnation(
                        connection, STORE, DIGEST, binding
                    )
            [(statement, arguments)] = reads
            raw = (await connection.exec_driver_sql(statement, arguments)).one()
            projected.append(tuple(raw))
            locks.require_existing_storage_address(connection, STORE, DIGEST)

    assert projected == [(None,)] * len(invalid)


@pytest.mark.asyncio
async def test_stage_fence_native_hit_misses_and_execute_only_bindings(
    database: Database,
) -> None:
    binding = await prepare_fence(database)
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
    prefix = (
        "EXPLAIN QUERY PLAN "
        if database.dialect is Dialect.SQLITE
        else "EXPLAIN (COSTS OFF) "
    )
    defaults: list[dict[str, object]] = []

    def observe(
        connection: object,
        cursor: object,
        statement: str,
        parameters: tuple[object, ...],
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        if address_statement_key(statement) and context.compiled is not None:
            assert context.compiled.params is not None
            defaults.append(dict(context.compiled.params))

    plans: list[str] = []
    rows: list[list[tuple[object, ...]]] = []
    async with database.transaction() as connection:
        locks = await lock_stage(connection, binding)
        event.listen(connection.sync_connection, "before_cursor_execute", observe)
        try:
            with executed_queries(connection) as queries:
                incarnation = await locks.read_stage_fence_incarnation(
                    connection, STORE, DIGEST, binding
                )
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", observe)
        [(statement, arguments)] = queries
        for search in (
            arguments,
            tuple(STORE.lower() if value == STORE else value for value in arguments),
            tuple(OTHER_DIGEST if value == DIGEST else value for value in arguments),
        ):
            plans.append(
                str(
                    (await connection.exec_driver_sql(prefix + statement, search)).all()
                )
            )
            rows.append(
                [
                    tuple(row)
                    for row in (
                        await connection.exec_driver_sql(statement, search)
                    ).all()
                ]
            )

    assert rows == [[(incarnation,)], [], []]
    assert STORE in arguments
    assert DIGEST in arguments
    assert len(defaults) == 1
    assert STORE not in defaults[0].values()
    assert DIGEST not in defaults[0].values()
    assert "FOR UPDATE" not in statement
    for plan in plans:
        if database.dialect is Dialect.SQLITE:
            assert "SEARCH storage_address_fence" in plan
            assert "store_id=? AND payload_digest=?" in plan
        else:
            assert "uq_storage_address_fence_store_id_payload_digest" in plan
            assert "Index Cond:" in plan
            assert "store_id =" in plan
            assert "payload_digest =" in plan


@pytest.mark.asyncio
async def test_cancelled_stage_fence_read_invalidates_guard_and_releases_writer(
    database: Database,
) -> None:
    binding = await prepare_fence(database)
    ledger = ConnectionLedger(database)
    entered = asyncio.Event()
    unreleased = asyncio.Event()

    def pause(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if address_statement_key(statement):
            entered.set()
            await_(asyncio.wait_for(unreleased.wait(), DEADLINE))

    async def read_until_cancelled() -> None:
        async with database.transaction() as connection:
            locks = await lock_stage(connection, binding)
            event.listen(connection.sync_connection, "before_cursor_execute", pause)
            try:
                await locks.read_stage_fence_incarnation(
                    connection, STORE, DIGEST, binding
                )
            except asyncio.CancelledError:
                with pytest.raises(
                    (LockError, WriterTransactionError),
                    match="cannot be used|not current",
                ):
                    locks.require_existing_storage_address(connection, STORE, DIGEST)
                raise
            finally:
                event.remove(connection.sync_connection, "before_cursor_execute", pause)

    reader = asyncio.create_task(read_until_cancelled())
    try:
        await asyncio.wait_for(entered.wait(), DEADLINE)
    finally:
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(reader, DEADLINE)
    async with database.transaction() as connection:
        locks = await lock_stage(connection, binding)
        incarnation = await locks.read_stage_fence_incarnation(
            connection, STORE, DIGEST, binding
        )

    assert incarnation == 1
    assert ledger.outstanding == 0
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
