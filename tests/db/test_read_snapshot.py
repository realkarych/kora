import asyncio
from collections.abc import AsyncIterator
from sqlite3 import SQLITE_READONLY

import pytest
import pytest_asyncio
from sqlalchemy import event, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.tables import namespace
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    ConnectionLedger,
    narrow_settings,
)
from tests.db.seed import Seeder


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
