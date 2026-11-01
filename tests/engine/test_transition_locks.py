import asyncio
from typing import Literal

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, DatabaseClosedError, Dialect, Lifecycle
from kora.db.tables import namespace
from kora.db.transactions import WRITER_LEASE, WriterTransactionError
from kora.engine.locks import RUN_CLASS, LockError, Scope, ScopeKind, TransitionLocks
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.lock_support import (
    DEADLINE,
    RUN_A,
    RUN_B,
    DeliberateRollback,
    acquire_second_run,
    foreign_assertion,
    held_transaction,
    lock_inventory,
    observed_locks,
    release_and_join,
    transaction_revision,
    wait_for_lock_waiter,
)


@pytest.mark.asyncio
async def test_transition_lock_order_and_identity(database: Database) -> None:
    scopes = (
        Scope(ScopeKind.TARGET, ("ab", "c")),
        Scope(ScopeKind.NAMESPACE, ("namespace-a",)),
        Scope(ScopeKind.GLOBAL),
        Scope(ScopeKind.TARGET, ("a", "bc")),
        Scope(ScopeKind.GRAPH, ("graph",)),
        Scope(ScopeKind.NAMESPACE, ("namespace-a",)),
    )
    expected = [(RUN_CLASS, -1708096332)] + sorted({scope.key for scope in scopes})

    async with observed_locks(database) as observer:
        async with database.transaction() as connection:
            locks = TransitionLocks(connection)
            assert await locks.acquire_run(RUN_A)
            await locks.acquire_scopes(scopes)
            locks.require(RUN_A, scopes)
            assert await locks.acquire_run(RUN_A)
            with pytest.raises(LockError, match="first and once"):
                await locks.acquire_run(RUN_B)
            with pytest.raises(LockError, match="acquired once"):
                await locks.acquire_scopes(())
            with pytest.raises(LockError, match="not held"):
                locks.require(RUN_A, (Scope(ScopeKind.GRAPH, ("different",)),))

    assert Scope(ScopeKind.GLOBAL).key == (2, 1330892193)
    assert scopes[1].key == (3, 1107856913)
    assert scopes[0].key == (7, -2108152575)
    assert scopes[3].key == (7, 1118237684)
    assert observer.keys == (expected if database.dialect is Dialect.POSTGRESQL else [])
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_scope_only_transaction_cannot_take_a_run(database: Database) -> None:
    scope = Scope(ScopeKind.NAMESPACE, ("namespace",))

    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_scopes((scope,))
        locks.require(None, (scope,))
        with pytest.raises(LockError, match="first and once"):
            await locks.acquire_run(RUN_A)

    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_transition_locks_require_live_writer(database: Database) -> None:
    async with database.read_transaction() as reader:
        with pytest.raises(WriterTransactionError, match="managed writer"):
            TransitionLocks(reader)
    async with database.writer_engine.connect() as unmanaged:
        async with unmanaged.begin():
            with pytest.raises(WriterTransactionError, match="managed writer"):
                TransitionLocks(unmanaged)

    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_run(RUN_A)
        with pytest.raises(WriterTransactionError, match="already has"):
            TransitionLocks(connection)
        with pytest.raises(WriterTransactionError, match="not current"):
            await asyncio.create_task(foreign_assertion(locks))
        async with connection.begin_nested():
            with pytest.raises(WriterTransactionError, match="not current"):
                locks.require(RUN_A)
        locks.require(RUN_A)
        storage = connection.info
    with pytest.raises(WriterTransactionError, match="not current"):
        locks.require(RUN_A)

    assert WRITER_LEASE not in storage
    assert database.writer_is_free()
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["commit", "rollback"])
async def test_replaced_transaction_cannot_reuse_locks(
    database: Database, ending: Literal["commit", "rollback"]
) -> None:
    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_run(RUN_A)

        if ending == "commit":
            await connection.commit()
        else:
            await connection.rollback()
        await connection.begin()
        with pytest.raises(WriterTransactionError, match="not current"):
            locks.require(RUN_A)
        with pytest.raises(WriterTransactionError, match="not current"):
            TransitionLocks(connection)

    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_run_locks_serialize_and_try_refuses(database: Database) -> None:
    ready = asyncio.Event()
    release = asyncio.Event()
    owner = asyncio.create_task(held_transaction(database, ready, release))
    await asyncio.wait_for(ready.wait(), DEADLINE)
    try_refused = False

    try:
        if database.dialect is Dialect.POSTGRESQL:
            async with database.transaction() as connection:
                locks = TransitionLocks(connection)
                try_refused = not await locks.acquire_run(RUN_A, wait=False)
                with pytest.raises(LockError, match="cannot be used"):
                    locks.require(RUN_A)
                with pytest.raises(LockError, match="cannot be used"):
                    await locks.acquire_run(RUN_B)
                await connection.rollback()
        follower = asyncio.create_task(acquire_second_run(database))
        await wait_for_lock_waiter(database)
        waiting = not follower.done()
    finally:
        release.set()
        await asyncio.wait_for(owner, DEADLINE)
    await asyncio.wait_for(follower, DEADLINE)

    assert waiting
    assert try_refused == (database.dialect is Dialect.POSTGRESQL)
    assert await lock_inventory(database) == []
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_scope_locks_serialize_fresh_reads(database: Database) -> None:
    async with database.transaction() as connection:
        namespace_id = await Seeder(connection).namespace()
    scope = Scope(ScopeKind.NAMESPACE, (namespace_id,))
    ready = asyncio.Event()
    release = asyncio.Event()
    observations: list[int] = []

    async def holder() -> None:
        async with database.transaction() as connection:
            locks = TransitionLocks(connection)
            await locks.acquire_run(RUN_A)
            await locks.acquire_scopes((scope,))
            await connection.execute(update(namespace).values(policy_revision=2))
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)

    async def contender() -> None:
        async with database.transaction() as connection:
            locks = TransitionLocks(connection)
            await locks.acquire_run(RUN_B)
            observations.append(await transaction_revision(connection))
            await locks.acquire_scopes((scope,))
            observations.append(await transaction_revision(connection))
            locks.require(RUN_B, (scope,))
            if database.dialect is Dialect.POSTGRESQL:
                isolation = await connection.scalar(text("SHOW transaction_isolation"))
                assert isolation == "read committed"

    owner = asyncio.create_task(holder())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    follower = asyncio.create_task(contender())
    try:
        await wait_for_lock_waiter(database)
    finally:
        await release_and_join(release, owner, follower)

    assert observations == (
        [1, 2] if database.dialect is Dialect.POSTGRESQL else [2, 2]
    )
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["commit", "rollback", "cancel"])
async def test_transition_lock_lifetime(
    database: Database, ending: Literal["commit", "rollback", "cancel"]
) -> None:
    ledger = ConnectionLedger(database)
    ready = asyncio.Event()
    release = asyncio.Event()
    leases: list[dict[str, object]] = []

    async def owner() -> None:
        async with database.transaction() as connection:
            leases.append(connection.info)
            locks = TransitionLocks(connection)
            await locks.acquire_run(RUN_A)
            await locks.acquire_scopes((Scope(ScopeKind.GLOBAL),))
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)
            if ending == "rollback":
                raise DeliberateRollback()

    task = asyncio.create_task(owner())
    await asyncio.wait_for(ready.wait(), DEADLINE)

    if ending == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        if ending == "rollback":
            with pytest.raises(DeliberateRollback):
                await task
        else:
            await task
    await acquire_second_run(database)

    assert ledger.outstanding == 0
    assert ledger.checkouts >= 2
    assert all(WRITER_LEASE not in storage for storage in leases)
    assert await lock_inventory(database) == []
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_cancelled_lock_wait_releases_partial_acquisition(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    ready = asyncio.Event()
    release = asyncio.Event()
    shared = Scope(ScopeKind.NAMESPACE, ("shared",))
    owner = asyncio.create_task(held_transaction(database, ready, release, (shared,)))
    await asyncio.wait_for(ready.wait(), DEADLINE)
    contexts: list[TransitionLocks] = []

    async def contender() -> None:
        async with database.transaction() as connection:
            locks = TransitionLocks(connection)
            contexts.append(locks)
            await locks.acquire_run(RUN_B)
            await locks.acquire_scopes((Scope(ScopeKind.GLOBAL), shared))

    follower = asyncio.create_task(contender())
    try:
        await wait_for_lock_waiter(database)
        follower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await follower
        if database.dialect is Dialect.POSTGRESQL:
            remaining = await lock_inventory(database)
            assert sorted(kind for kind, _, granted in remaining if granted) == [1, 3]
        for locks in contexts:
            with pytest.raises(WriterTransactionError, match="not current"):
                locks.require(RUN_B)
    finally:
        await release_and_join(release, owner)

    assert ledger.outstanding == 0
    assert await lock_inventory(database) == []
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_partial_acquisition_failure_invalidates_context(
    database: Database,
) -> None:
    if database.dialect is Dialect.SQLITE:
        async with database.transaction() as connection:
            locks = TransitionLocks(connection)
            await locks.acquire_run(RUN_A)
            await locks.acquire_scopes((Scope(ScopeKind.GLOBAL),))
            locks.require(RUN_A)
        assert database.writer_is_free()
        return
    ready = asyncio.Event()
    release = asyncio.Event()
    shared = Scope(ScopeKind.NAMESPACE, ("shared",))
    owner = asyncio.create_task(held_transaction(database, ready, release, (shared,)))
    await asyncio.wait_for(ready.wait(), DEADLINE)

    try:
        async with database.transaction() as connection:
            locks = TransitionLocks(connection)
            await locks.acquire_run(RUN_B)
            await connection.execute(text("SET LOCAL lock_timeout = '50ms'"))
            with pytest.raises(DBAPIError):
                await locks.acquire_scopes((Scope(ScopeKind.GLOBAL), shared))
            with pytest.raises(LockError, match="cannot be used"):
                locks.require(RUN_B)
            await connection.rollback()
    finally:
        await release_and_join(release, owner)

    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_shielded_unit_retains_locks_until_completion(database: Database) -> None:
    ready = asyncio.Event()
    release = asyncio.Event()
    cancellation_turn = asyncio.Event()
    ledger = ConnectionLedger(database)

    async def unit(connection: AsyncConnection) -> None:
        locks = TransitionLocks(connection)
        await locks.acquire_run(RUN_A)
        ready.set()
        await asyncio.wait_for(release.wait(), DEADLINE)
        locks.require(RUN_A)
        await Seeder(connection).namespace("committed-after-cancellation")

    task = asyncio.create_task(database.run(unit))
    await asyncio.wait_for(ready.wait(), DEADLINE)

    task.cancel()
    asyncio.get_running_loop().call_soon(cancellation_turn.set)
    await asyncio.wait_for(cancellation_turn.wait(), DEADLINE)
    still_owned = database.units_in_flight() == 1 and ledger.outstanding == 1
    returned_early = task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, DEADLINE)
    async with database.read_transaction() as connection:
        revision = await transaction_revision(connection)

    assert still_owned
    assert not returned_early
    assert revision == 1
    assert ledger.outstanding == 0
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_shutdown_keeps_connection_owned_until_unit_exits(
    database: Database,
) -> None:
    ready = asyncio.Event()
    release = asyncio.Event()
    ledger = ConnectionLedger(database)
    owner = asyncio.create_task(held_transaction(database, ready, release))
    await asyncio.wait_for(ready.wait(), DEADLINE)

    await database.close(deadline_seconds=0)
    retained = ledger.outstanding
    closed_early = ledger.closed_while_checked_out
    release.set()
    with pytest.raises(DatabaseClosedError, match="closed before"):
        await asyncio.wait_for(owner, DEADLINE)

    assert database.lifecycle is Lifecycle.CLOSED
    assert retained == 1
    assert closed_early == 0
    assert ledger.outstanding == 0
    assert ledger.opened == ledger.closed
    assert database.writer_is_free()
    reopened = Database(database.settings)
    try:
        await acquire_second_run(reopened)
        assert await lock_inventory(reopened) == []
    finally:
        await reopened.close()
