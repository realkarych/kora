import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Dialect
from kora.engine.locks import Scope, TransitionLocks
from tests.db.closing import until

RUN_A: str = "00000000000000000000000001"
RUN_B: str = "00000000000000000000000002"
DEADLINE: float = 10.0


class DeliberateRollback(RuntimeError):
    pass


class LockStatements:
    def __init__(self) -> None:
        self.keys: list[tuple[int, int]] = []

    def observe(
        self,
        connection: object,
        cursor: object,
        statement: str,
        parameters: tuple[int, int],
        context: object,
        executemany: bool,
    ) -> None:
        if "pg_advisory_xact_lock" in statement:
            self.keys.append((parameters[0], parameters[1]))


@asynccontextmanager
async def observed_locks(database: Database) -> AsyncGenerator[LockStatements, None]:
    observer = LockStatements()
    engine = database.writer_engine.sync_engine
    event.listen(engine, "before_cursor_execute", observer.observe)
    try:
        yield observer
    finally:
        event.remove(engine, "before_cursor_execute", observer.observe)


async def lock_inventory(database: Database) -> list[tuple[int, int, bool]]:
    if database.dialect is Dialect.SQLITE:
        return []
    async with database.read_transaction() as connection:
        result = await connection.execute(
            text(
                "SELECT classid, objid, granted FROM pg_locks "
                "WHERE locktype = 'advisory' AND objsubid = 2 "
                "AND database = (SELECT oid FROM pg_database "
                "WHERE datname = current_database())"
            )
        )
        try:
            return [(int(row[0]), int(row[1]), bool(row[2])) for row in result]
        finally:
            result.close()


async def wait_for_lock_waiter(database: Database) -> None:
    if database.dialect is Dialect.SQLITE:
        await until(lambda: database.units_in_flight() == 2)
        return
    async with asyncio.timeout(DEADLINE):
        while not any(not granted for _, _, granted in await lock_inventory(database)):
            pass


async def held_transaction(
    database: Database,
    ready: asyncio.Event,
    release: asyncio.Event,
    scopes: tuple[Scope, ...] = (),
) -> None:
    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_run(RUN_A)
        await locks.acquire_scopes(scopes)
        ready.set()
        await asyncio.wait_for(release.wait(), DEADLINE)
        locks.require(RUN_A, scopes)


async def acquire_second_run(database: Database, run_id: str = RUN_A) -> None:
    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        assert await locks.acquire_run(run_id)
        locks.require(run_id)


async def release_and_join(release: asyncio.Event, *tasks: asyncio.Task[None]) -> None:
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), DEADLINE)


async def foreign_assertion(locks: TransitionLocks) -> None:
    locks.require(RUN_A)


async def transaction_revision(connection: AsyncConnection) -> int:
    from sqlalchemy import select

    from kora.db.tables import namespace

    return int(
        (await connection.execute(select(namespace.c.policy_revision))).scalar_one()
    )
