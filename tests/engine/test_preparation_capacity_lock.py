import asyncio
from typing import Literal

import pytest
from sqlalchemy import event, text
from sqlalchemy.util import await_

from kora.db.connection import Database, Dialect
from kora.db.tables import graph_deployment_preparation_capacity
from kora.db.transactions import WriterTransactionError
from kora.engine.locks import (
    RUN_CLASS,
    LockError,
    PreparationCapacityUnavailable,
    Scope,
    ScopeKind,
    TransitionLocks,
)
from kora.protocols.identity_records import MAX_COUNT
from tests.db.closing import ConnectionLedger
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.lock_support import (
    DEADLINE,
    RUN_A,
    DeliberateRollback,
    lock_inventory,
    observed_locks,
    wait_for_lock_waiter,
)

CAPACITY: Scope = Scope(ScopeKind.PREPARATION_CAPACITY)
GLOBAL: Scope = Scope(ScopeKind.GLOBAL)
NAMESPACE: Scope = Scope(ScopeKind.NAMESPACE, ("capacity-namespace",))


async def insert_ledger(database: Database, occupied: int) -> None:
    async with database.transaction() as connection:
        await connection.execute(
            graph_deployment_preparation_capacity.insert().values(
                id=1, occupied=occupied
            )
        )


@pytest.mark.asyncio
async def test_preparation_capacity_scope_is_distinct_and_ordered(
    database: Database,
) -> None:
    await insert_ledger(database, MAX_COUNT)
    scopes = (NAMESPACE, GLOBAL, CAPACITY, GLOBAL, CAPACITY)
    async with database.transaction() as writer:
        ordinary = TransitionLocks(writer)
        await ordinary.acquire_scopes((GLOBAL,))
        with executed_queries(writer) as premature:
            with pytest.raises(LockError, match="locks are not held"):
                await ordinary.read_preparation_capacity(writer)

    async with observed_locks(database) as observed:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            assert await locks.acquire_run(RUN_A)
            await locks.acquire_scopes(scopes)
            with executed_queries(writer) as reads:
                occupied = await locks.read_preparation_capacity(writer)
                with pytest.raises(WriterTransactionError, match="not current"):
                    await asyncio.create_task(locks.read_preparation_capacity(writer))
                async with database.read_transaction() as reader:
                    with pytest.raises(LockError, match="does not own"):
                        await locks.read_preparation_capacity(reader)
            [(statement, arguments)] = reads
            raw = (await writer.exec_driver_sql(statement, arguments)).one()
    with pytest.raises(WriterTransactionError, match="not current"):
        await locks.read_preparation_capacity(writer)
    with pytest.raises(LockError, match="invalid lock scope"):
        Scope(ScopeKind.PREPARATION_CAPACITY, ("different",))

    assert CAPACITY.key == (0, 1330892193)
    assert GLOBAL.key == (2, 1330892193)
    assert observed.keys == (
        [(RUN_CLASS, -1708096332), CAPACITY.key, GLOBAL.key, NAMESPACE.key]
        if database.dialect is Dialect.POSTGRESQL
        else []
    )
    assert premature == []
    assert "WHERE graph_deployment_preparation_capacity.id =" in statement
    assert "FOR UPDATE" not in statement
    assert occupied == MAX_COUNT
    assert raw == (MAX_COUNT,)
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_missing_and_malformed_capacity_refuse_the_current_guard(
    database: Database,
) -> None:
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_scopes((CAPACITY,))
        with executed_queries(writer) as missing:
            with pytest.raises(PreparationCapacityUnavailable):
                await locks.read_preparation_capacity(writer)
        [(statement, arguments)] = missing
        missing_projection = (await writer.exec_driver_sql(statement, arguments)).all()
        with pytest.raises(LockError, match="cannot be used"):
            locks.require(None, (CAPACITY,))
    await insert_ledger(database, 0)
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_scopes((CAPACITY,))
        if database.dialect is Dialect.SQLITE:
            await writer.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            try:
                await writer.exec_driver_sql(
                    "UPDATE graph_deployment_preparation_capacity SET occupied = 0.5"
                )
            finally:
                await writer.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
            storage_class = await writer.scalar(
                text(
                    "SELECT typeof(occupied) FROM graph_deployment_preparation_capacity"
                )
            )
            assert storage_class == "real"
        else:
            await writer.exec_driver_sql(
                "ALTER TABLE graph_deployment_preparation_capacity DROP CONSTRAINT "
                "ck_graph_deployment_preparation_capacity_nonnegative"
            )
            await writer.exec_driver_sql(
                "UPDATE graph_deployment_preparation_capacity SET occupied = -1"
            )

        with executed_queries(writer) as malformed:
            with pytest.raises(PreparationCapacityUnavailable):
                await locks.read_preparation_capacity(writer)
        [(statement, arguments)] = malformed
        malformed_projection = (
            await writer.exec_driver_sql(statement, arguments)
        ).one()
        with pytest.raises(LockError, match="cannot be used"):
            locks.require(None, (CAPACITY,))

    assert missing_projection == []
    assert malformed_projection == (None,)


async def read_capacity(database: Database) -> int:
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_scopes((CAPACITY,))
        return await locks.read_preparation_capacity(writer)


async def ordinary_scope(database: Database) -> None:
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_scopes((GLOBAL,))
        locks.require(None, (GLOBAL,))


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["commit", "rollback", "cancel"])
async def test_preparation_capacity_waiter_reads_after_release(
    database: Database, ending: Literal["commit", "rollback", "cancel"]
) -> None:
    await insert_ledger(database, 0)
    ledger = ConnectionLedger(database)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_scopes((CAPACITY,))
            await writer.execute(
                graph_deployment_preparation_capacity.update().values(occupied=1)
            )
            entered.set()
            await asyncio.wait_for(release.wait(), DEADLINE)
            if ending == "rollback":
                raise DeliberateRollback()

    holder = asyncio.create_task(hold())
    follower: asyncio.Task[int] | None = None
    occupied: int | None = None
    try:
        await asyncio.wait_for(entered.wait(), DEADLINE)
        follower = asyncio.create_task(read_capacity(database))
        await wait_for_lock_waiter(database)
        assert not follower.done()
        if database.dialect is Dialect.POSTGRESQL:
            await asyncio.wait_for(ordinary_scope(database), DEADLINE)
    finally:
        if ending == "cancel":
            holder.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(holder, DEADLINE)
        else:
            release.set()
            if ending == "rollback":
                with pytest.raises(DeliberateRollback):
                    await asyncio.wait_for(holder, DEADLINE)
            else:
                await asyncio.wait_for(holder, DEADLINE)
        if follower is not None:
            occupied = await asyncio.wait_for(follower, DEADLINE)

    assert follower is not None
    assert occupied == (1 if ending == "commit" else 0)
    assert await lock_inventory(database) == []
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert ledger.outstanding == 0


@pytest.mark.asyncio
async def test_cancelled_capacity_read_invalidates_its_guard(
    database: Database,
) -> None:
    await insert_ledger(database, 0)
    ledger = ConnectionLedger(database)
    reading = asyncio.Event()
    unfinished = asyncio.Event()

    def pause(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if (
            "CASE WHEN" in statement
            and "FROM graph_deployment_preparation_capacity" in statement
        ):
            reading.set()
            await_(asyncio.wait_for(unfinished.wait(), DEADLINE))

    async def read_until_cancelled() -> None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_scopes((CAPACITY,))
            event.listen(writer.sync_connection, "before_cursor_execute", pause)
            try:
                await locks.read_preparation_capacity(writer)
            except asyncio.CancelledError:
                with pytest.raises(
                    (LockError, WriterTransactionError),
                    match="cannot be used|not current",
                ):
                    locks.require(None, (CAPACITY,))
                raise
            finally:
                event.remove(writer.sync_connection, "before_cursor_execute", pause)

    reader = asyncio.create_task(read_until_cancelled())
    try:
        await asyncio.wait_for(reading.wait(), DEADLINE)
    finally:
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(reader, DEADLINE)
    occupied = await asyncio.wait_for(read_capacity(database), DEADLINE)

    assert occupied == 0
    assert await lock_inventory(database) == []
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
    assert ledger.outstanding == 0
