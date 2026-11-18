import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from aiosqlite import Cursor as SQLiteCursor
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncMappingResult

from kora.db.approval_cost_reads import cost_attempt_statement, cost_rows
from kora.db.connection import Database, Dialect
from kora.engine.approval_cost import calculate_approval_cost
from kora.engine.locks import TransitionLocks
from kora.ir.cost import RunCost
from tests.db.closing import ConnectionLedger
from tests.db.seed import START
from tests.engine.approval_cost_support import (
    cost_attempt,
    cost_case,
    cost_usage,
    coverage,
)


class InterruptedCalculation(RuntimeError):
    pass


@dataclass
class CostCursors:
    adapters: list[object] = field(default_factory=list[object])
    sqlite: list[SQLiteCursor] = field(default_factory=list[SQLiteCursor])
    cancel: asyncio.Task[RunCost] | None = None

    def observe(
        self,
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if not (
            "FROM attempt JOIN node_instance" in statement
            or "FROM usage_record JOIN attempt" in statement
        ):
            return
        self.adapters.append(cursor)
        native: object = getattr(cursor, "_cursor", None)
        if isinstance(native, SQLiteCursor):
            self.sqlite.append(native)
        if self.cancel is not None:
            self.cancel.cancel()


@asynccontextmanager
async def observed_cursors(database: Database) -> AsyncGenerator[CostCursors]:
    observer = CostCursors()
    engine = database.writer_engine.sync_engine
    event.listen(engine, "after_cursor_execute", observer.observe)
    try:
        yield observer
    finally:
        event.remove(engine, "after_cursor_execute", observer.observe)


async def portals(connection: AsyncConnection) -> int:
    if connection.dialect.name != "postgresql":
        return 0
    with await connection.execute(
        text("SELECT count(*) FROM pg_cursors WHERE name LIKE '__asyncpg_portal_%'")
    ) as result:
        return int(result.scalar_one())


async def assert_native_closed(observer: CostCursors) -> None:
    assert observer.adapters
    for adapter in observer.adapters:
        assert getattr(adapter, "_cursor", None) is None
    for cursor in observer.sqlite:
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            await cursor.fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["commit", "error", "cancel", "shutdown"])
async def test_approval_cost_cursor_release_on_every_exit(
    database: Database, ending: str
) -> None:
    case = await cost_case(database)
    async with database.transaction() as writer:
        await cost_attempt(writer, case, "agent", saved_coverage=coverage())
    ready = asyncio.Event()
    release = asyncio.Event()
    retained: list[AsyncMappingResult] = []
    native_portals: list[int] = []
    ledger = ConnectionLedger(database)

    async def calculate() -> None:
        async with database.transaction() as connection:
            locks = TransitionLocks(connection)
            await locks.acquire_run(case.run_id)
            async with cost_rows(
                connection,
                cost_attempt_statement(connection, case.namespace_id, case.run_id),
            ) as rows:
                retained.append(rows)
                assert len(await rows.fetchmany(1)) == 1
                native_portals.append(await portals(connection))
                ready.set()
                await release.wait()
                if ending == "error":
                    raise InterruptedCalculation()

    async with observed_cursors(database) as observer:
        task = asyncio.create_task(calculate())
        try:
            await asyncio.wait_for(ready.wait(), 10)
            if ending in {"cancel", "shutdown"}:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 10)
            elif ending == "error":
                release.set()
                with pytest.raises(InterruptedCalculation):
                    await asyncio.wait_for(task, 10)
            else:
                release.set()
                await asyncio.wait_for(task, 10)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    assert len(retained) == 1
    assert retained[0].closed
    await assert_native_closed(observer)
    if ending == "shutdown":
        await database.close()
    assert native_portals == ([1] if database.dialect is Dialect.POSTGRESQL else [0])
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
    reopened = Database(database.settings) if ending == "shutdown" else database
    try:
        async with reopened.transaction() as connection:
            locks = TransitionLocks(connection)
            assert await locks.acquire_run(case.run_id)
            assert await portals(connection) == 0
    finally:
        if reopened is not database:
            await reopened.close()


@pytest.mark.asyncio
async def test_approval_cost_cancelled_acquisition_closes_delivered_cursor(
    database: Database,
) -> None:
    case = await cost_case(database)
    async with database.transaction() as writer:
        _, identity = await cost_attempt(
            writer, case, "agent", saved_coverage=coverage()
        )
        await cost_usage(writer, identity)
    ledger = ConnectionLedger(database)

    async with observed_cursors(database) as observer:
        task = asyncio.create_task(case.calculate(database))
        observer.cancel = task
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)

    await assert_native_closed(observer)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
    recovered = await case.calculate(database)
    assert recovered.usd_estimate == "0.1"


@pytest.mark.asyncio
async def test_approval_cost_native_portals_end_with_the_caller_transaction(
    database: Database,
) -> None:
    case = await cost_case(database)
    async with database.transaction() as writer:
        _, identity = await cost_attempt(
            writer, case, "agent", saved_coverage=coverage()
        )
        await cost_usage(writer, identity)

    async with observed_cursors(database) as observer:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(case.run_id)
            result = await calculate_approval_cost(
                writer, locks, case.namespace_id, case.run_id, START
            )
            await assert_native_closed(observer)
            retained_portals = await portals(writer)
        async with database.transaction() as writer:
            remaining_portals = await portals(writer)

    assert len(observer.adapters) == 2
    assert retained_portals == (2 if database.dialect is Dialect.POSTGRESQL else 0)
    assert remaining_portals == 0
    assert result.usd_estimate == "0.1"
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
