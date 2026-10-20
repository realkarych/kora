import asyncio
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry, PoolProxiedConnection

from kora.db.connection import Database, Dialect
from kora.db.tables import namespace
from tests.db.seed import Seeder

SLOW_SQLITE_STATEMENT: str = (
    "WITH RECURSIVE counter(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM counter "
    "WHERE n < 30000000) SELECT count(*) FROM counter"
)
SLOW_POSTGRESQL_STATEMENT: str = "SELECT pg_sleep(30)"
BARRIER_SECONDS: float = 10.0


class Fault(RuntimeError):
    pass


def slow_statement(database: Database) -> str:
    if database.dialect is Dialect.SQLITE:
        return SLOW_SQLITE_STATEMENT
    return SLOW_POSTGRESQL_STATEMENT


class CheckoutCounter:
    def __init__(self, database: Database) -> None:
        self.outstanding = 0
        self.checkouts = 0
        engines = {database.writer_engine, database.reader_engine}
        for engine in engines:
            event.listen(engine.sync_engine, "checkout", self._checkout)
            event.listen(engine.sync_engine, "checkin", self._checkin)

    def _checkout(
        self,
        raw_connection: DBAPIConnection,
        record: ConnectionPoolEntry,
        proxy: PoolProxiedConnection,
    ) -> None:
        self.outstanding += 1
        self.checkouts += 1

    def _checkin(
        self, raw_connection: DBAPIConnection | None, record: ConnectionPoolEntry
    ) -> None:
        self.outstanding -= 1


async def names(database: Database) -> list[str]:
    async with database.read_transaction() as connection:
        rows = (
            await connection.execute(
                select(namespace.c.name).order_by(namespace.c.name)
            )
        ).all()
    return [str(row[0]) for row in rows]


async def commit_path(database: Database) -> None:
    async with database.transaction() as connection:
        await Seeder(connection).namespace("committed")


async def rollback_path(database: Database) -> None:
    with pytest.raises(Fault):
        async with database.transaction() as connection:
            await Seeder(connection).namespace("rolled-back")
            raise Fault()


async def cancelled_in_flight_path(database: Database) -> None:
    async def work() -> None:
        async with database.transaction() as connection:
            await Seeder(connection).namespace("cancelled-in-flight")
            await connection.execute(text(slow_statement(database)))

    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    while not task.done() and database.units_in_flight() == 0:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def cancelled_while_waiting_path(database: Database) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with database.transaction() as connection:
            await Seeder(connection).namespace("holder")
            entered.set()
            await asyncio.wait_for(release.wait(), BARRIER_SECONDS)

    async def waiter() -> None:
        async with database.transaction() as connection:
            await Seeder(connection).namespace("waiter")

    holding = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), BARRIER_SECONDS)
    waiting = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    if database.dialect is Dialect.SQLITE:
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    release.set()
    await holding
    if database.dialect is Dialect.POSTGRESQL:
        await waiting


PATHS: list[Callable[[Database], Awaitable[None]]] = [
    commit_path,
    rollback_path,
    cancelled_in_flight_path,
    cancelled_while_waiting_path,
]


@pytest.mark.asyncio
async def test_transaction_releases_connection_and_writer_on_every_path(
    database: Database,
) -> None:
    counter = CheckoutCounter(database)
    for path in PATHS:
        await path(database)

        assert counter.outstanding == 0, path.__name__
        assert database.writer_is_free(), path.__name__
        async with database.transaction() as connection:
            in_transaction = await connection.run_sync(
                lambda sync: sync.in_transaction()
            )
            assert in_transaction, path.__name__

    expected = ["committed", "holder"]
    if database.dialect is Dialect.POSTGRESQL:
        expected.append("waiter")
    assert await names(database) == expected
    assert counter.checkouts >= len(PATHS)


@pytest.mark.asyncio
async def test_sqlite_serialises_concurrent_writers(database: Database) -> None:
    if database.dialect is not Dialect.SQLITE:
        async with database.transaction() as one:
            async with database.transaction() as two:
                await Seeder(one).namespace("first")
                await Seeder(two).namespace("second")
        assert await names(database) == ["first", "second"]
        return
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with database.transaction() as connection:
            await Seeder(connection).namespace("first")
            first_entered.set()
            await asyncio.wait_for(release_first.wait(), BARRIER_SECONDS)
            order.append("first committed")

    async def second() -> None:
        async with database.transaction() as connection:
            order.append("second entered")
            await Seeder(connection).namespace("second")

    first_task = asyncio.create_task(first())
    await asyncio.wait_for(first_entered.wait(), BARRIER_SECONDS)
    second_task = asyncio.create_task(second())
    for _ in range(10):
        await asyncio.sleep(0)
    entered_early = "second entered" in order
    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert not entered_early
    assert order == ["first committed", "second entered"]
    assert await names(database) == ["first", "second"]


@pytest.mark.asyncio
async def test_sqlite_reads_proceed_while_the_writer_is_held(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        await Seeder(connection).namespace("before")
    async with database.transaction() as writer:
        await Seeder(writer).namespace("uncommitted")

        seen = await names(database)

        assert seen == ["before"]
        assert not database.writer_is_free() or database.dialect is Dialect.POSTGRESQL
    assert await names(database) == ["before", "uncommitted"]


@pytest.mark.asyncio
async def test_close_waits_for_the_unit_in_flight_then_refuses_new_work(
    database: Database,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with database.transaction() as connection:
            await Seeder(connection).namespace("held")
            entered.set()
            await asyncio.wait_for(release.wait(), BARRIER_SECONDS)

    holding = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), BARRIER_SECONDS)
    closing = asyncio.create_task(database.close(deadline_seconds=BARRIER_SECONDS))
    for _ in range(10):
        await asyncio.sleep(0)
    closed_early = closing.done()
    release.set()
    await holding
    await closing

    assert not closed_early
    with pytest.raises(RuntimeError):
        async with database.transaction():
            pass
    with pytest.raises(RuntimeError):
        async with database.read_transaction():
            pass
