import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace

import pytest
from sqlalchemy import Connection, event, select, text
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.pool import ConnectionPoolEntry, PoolProxiedConnection
from sqlalchemy.util import await_

from kora.db.connection import Database, DatabaseClosedError, Dialect, Lifecycle
from kora.db.tables import namespace
from tests.db.closing import (
    CLOSED_BEFORE_FINISHING,
    ConnectionLedger,
    failure_messages,
    hold_reader,
    hold_writer,
    narrow_database,
    sqlite_companions,
    until,
    write,
)
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


async def faulting_transaction(database: Database) -> None:
    async with database.transaction() as connection:
        await Seeder(connection).namespace("rolled-back")
        raise Fault()


async def rollback_path(database: Database) -> None:
    with pytest.raises(Fault):
        await faulting_transaction(database)


class StatementWatch:
    def __init__(self, database: Database, fragment: str) -> None:
        self.fragment = fragment
        self.started = asyncio.Event()
        event.listen(
            database.writer_engine.sync_engine, "before_cursor_execute", self._observe
        )

    def _observe(
        self,
        connection: object,
        cursor: object,
        statement: str,
        *arguments: object,
    ) -> None:
        if self.fragment in statement:
            self.started.set()


async def cancelled_in_flight_path(database: Database) -> None:
    watch = StatementWatch(
        database, "pg_sleep" if database.dialect is Dialect.POSTGRESQL else "counter"
    )

    async def work() -> None:
        async with database.transaction() as connection:
            await Seeder(connection).namespace("cancelled-in-flight")
            await connection.execute(text(slow_statement(database)))

    task = asyncio.create_task(work())
    await asyncio.wait_for(watch.started.wait(), BARRIER_SECONDS)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert watch.started.is_set()


async def cancelled_twice_path(database: Database) -> None:
    async def work() -> None:
        async with database.transaction() as connection:
            await Seeder(connection).namespace("cancelled-twice")
            await asyncio.sleep(BARRIER_SECONDS)

    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    while not task.done() and database.units_in_flight() == 0:
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def cancelled_waiting_for_the_pool_path(database: Database) -> None:
    if database.dialect is not Dialect.POSTGRESQL:
        return
    narrow = Database(
        replace(
            database.settings, pool_size=1, max_overflow=0, pool_timeout_seconds=0.2
        )
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    try:

        async def holder() -> None:
            async with narrow.transaction() as connection:
                await Seeder(connection).namespace("pool-holder")
                entered.set()
                await asyncio.wait_for(release.wait(), BARRIER_SECONDS)

        async def waiter() -> None:
            async with narrow.transaction() as connection:
                await Seeder(connection).namespace("pool-waiter")

        holding = asyncio.create_task(holder())
        await asyncio.wait_for(entered.wait(), BARRIER_SECONDS)
        waiting = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert waiting.cancelled()
        release.set()
        await holding
        assert narrow.units_in_flight() == 0
    finally:
        await narrow.close()


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
    cancelled_twice_path,
    cancelled_while_waiting_path,
    cancelled_waiting_for_the_pool_path,
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
        expected.extend(["pool-holder", "waiter"])
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

    async def queued() -> None:
        async with database.transaction() as connection:
            await Seeder(connection).namespace("queued")

    holding = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), BARRIER_SECONDS)
    waiting = asyncio.create_task(queued())
    await asyncio.sleep(0)
    closing = asyncio.create_task(database.close(deadline_seconds=BARRIER_SECONDS))
    for _ in range(10):
        await asyncio.sleep(0)
    closed_early = closing.done()
    in_flight_while_queued = database.units_in_flight()
    release.set()
    await holding
    await waiting
    await closing

    assert not closed_early
    assert in_flight_while_queued == 2
    assert await names_after_close(database) == ["held", "queued"]
    with pytest.raises(DatabaseClosedError):
        async with database.transaction():
            pass
    with pytest.raises(DatabaseClosedError):
        async with database.read_transaction():
            pass


async def names_after_close(database: Database) -> list[str]:
    reopened = Database(database.settings)
    try:
        return await names(reopened)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_run_shields_the_unit_from_cancellation(database: Database) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    finished: list[str] = []

    async def unit(connection: AsyncConnection) -> str:
        await Seeder(connection).namespace("shielded")
        entered.set()
        await asyncio.wait_for(release.wait(), BARRIER_SECONDS)
        finished.append("unit finished")
        return "done"

    task = asyncio.create_task(database.run(unit))
    await asyncio.wait_for(entered.wait(), BARRIER_SECONDS)
    task.cancel()
    await asyncio.sleep(0)
    cancelled_before_release = task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await database.run(lambda connection: names_through(connection))

    assert not cancelled_before_release
    assert finished == ["unit finished"]
    assert result == ["shielded"]
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


async def names_through(connection: AsyncConnection) -> list[str]:
    rows = (
        await connection.execute(select(namespace.c.name).order_by(namespace.c.name))
    ).all()
    return [str(row[0]) for row in rows]


@pytest.mark.asyncio
async def test_close_after_the_deadline_fails_every_unit_in_flight_and_writes_nothing(
    database: Database,
) -> None:
    closing = narrow_database(database)
    ledger = ConnectionLedger(closing)
    writer_entered = asyncio.Event()
    reader_entered = asyncio.Event()
    release = asyncio.Event()
    holding = asyncio.create_task(
        hold_writer(closing, "outlived", writer_entered, release)
    )
    await asyncio.wait_for(writer_entered.wait(), BARRIER_SECONDS)
    reading = asyncio.create_task(hold_reader(closing, reader_entered, release))
    await asyncio.wait_for(reader_entered.wait(), BARRIER_SECONDS)
    queued = asyncio.create_task(write(closing, "queued"))
    await until(lambda: closing.units_in_flight() == 3)
    opened_before_close = ledger.opened
    checkouts_before_close = ledger.checkouts

    await closing.close(deadline_seconds=0.2)
    release.set()
    results = await asyncio.gather(holding, reading, queued, return_exceptions=True)
    companions = sqlite_companions(closing)

    assert failure_messages(results) == [CLOSED_BEFORE_FINISHING] * 3
    assert await names_after_close(closing) == []
    assert ledger.opened == opened_before_close
    assert ledger.checkouts - checkouts_before_close == checkouts_of_a_queued_writer(
        closing
    )
    assert ledger.closed == ledger.opened
    assert ledger.closed_while_checked_out == 0
    assert ledger.outstanding == 0
    assert companions == []
    assert closing.units_in_flight() == 0
    assert closing.writer_is_free()


def checkouts_of_a_queued_writer(database: Database) -> int:
    if database.dialect is Dialect.SQLITE:
        return 0
    return 1


@pytest.mark.asyncio
async def test_close_while_a_commit_is_under_way_lets_the_commit_finish(
    database: Database,
) -> None:
    closing = narrow_database(database)
    ledger = ConnectionLedger(closing)
    lifecycles_at_commit: list[Lifecycle] = []
    closes: list[asyncio.Future[None]] = []

    def close_during_commit(connection: Connection) -> None:
        closes.append(asyncio.ensure_future(closing.close(deadline_seconds=0)))
        await_(until(lambda: closing.lifecycle is Lifecycle.CLOSED))
        lifecycles_at_commit.append(closing.lifecycle)

    event.listen(closing.writer_engine.sync_engine, "commit", close_during_commit)

    await write(closing, "committed-at-the-deadline")
    await closes[0]

    assert lifecycles_at_commit == [Lifecycle.CLOSED]
    assert await names_after_close(closing) == ["committed-at-the-deadline"]
    assert ledger.closed == ledger.opened
    assert ledger.closed_while_checked_out == 0
    assert ledger.outstanding == 0
    assert closing.units_in_flight() == 0


@pytest.mark.asyncio
async def test_close_during_the_checkin_of_a_committed_unit_fails_the_unit_behind_it(
    database: Database,
) -> None:
    closing = narrow_database(database, pool_size=1)
    ledger = ConnectionLedger(closing)
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    closes: list[asyncio.Future[None]] = []

    def close_during_checkin(*arguments: object) -> None:
        if not closes:
            closes.append(asyncio.ensure_future(closing.close(deadline_seconds=0)))

    event.listen(closing.writer_engine.sync_engine, "reset", close_during_checkin)
    first = asyncio.create_task(
        hold_writer(closing, "handed-off", first_entered, release_first)
    )
    await asyncio.wait_for(first_entered.wait(), BARRIER_SECONDS)
    second = asyncio.create_task(
        hold_writer(closing, "behind", second_entered, release_second)
    )
    await until(lambda: closing.units_in_flight() == 2)

    release_first.set()
    await first
    await closes[0]
    release_second.set()
    results = await asyncio.gather(second, return_exceptions=True)
    companions = sqlite_companions(closing)

    assert failure_messages(results) == [CLOSED_BEFORE_FINISHING]
    assert await names_after_close(closing) == ["handed-off"]
    assert ledger.closed == ledger.opened
    assert ledger.closed_while_checked_out == 0
    assert ledger.outstanding == 0
    assert companions == []
    assert closing.units_in_flight() == 0


@pytest.mark.asyncio
async def test_close_is_shared_by_every_caller_and_survives_their_cancellation(
    database: Database,
) -> None:
    closing = narrow_database(database)
    ledger = ConnectionLedger(closing)
    entered = asyncio.Event()
    release = asyncio.Event()
    holding = asyncio.create_task(hold_writer(closing, "outlived", entered, release))
    await asyncio.wait_for(entered.wait(), BARRIER_SECONDS)
    first = asyncio.create_task(closing.close(deadline_seconds=0.2))
    await until(lambda: closing.lifecycle is not Lifecycle.OPEN)

    first.cancel()
    await closing.close(deadline_seconds=0)
    lifecycle_when_second_returned = closing.lifecycle
    in_flight_when_second_returned = closing.units_in_flight()
    release.set()
    results = await asyncio.gather(holding, return_exceptions=True)
    first_results = await asyncio.gather(first, return_exceptions=True)

    assert lifecycle_when_second_returned is Lifecycle.CLOSED
    assert in_flight_when_second_returned == 1
    assert isinstance(first_results[0], asyncio.CancelledError)
    assert failure_messages(results) == [CLOSED_BEFORE_FINISHING]
    assert await names_after_close(closing) == []
    assert ledger.closed == ledger.opened
    assert ledger.closed_while_checked_out == 0
    assert ledger.outstanding == 0
