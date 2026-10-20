import asyncio
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from sqlalchemy import event, select

from kora.db.connection import (
    Database,
    DatabaseClosedError,
    DatabaseSettings,
    Dialect,
    Lifecycle,
)
from kora.db.tables import namespace
from tests.db.seed import Seeder

NARROW_POOL_SIZE: int = 2
NARROW_POOL_TIMEOUT_SECONDS: float = 30.0
CONDITION_DEADLINE_SECONDS: float = 10.0
REFUSAL_DEADLINE_SECONDS: float = 1.0
SQLITE_COMPANION_SUFFIXES: tuple[str, ...] = ("-wal", "-shm")
CLOSED_BEFORE_FINISHING: str = "the database closed before the unit finished"


def narrow_settings(
    settings: DatabaseSettings, pool_size: int = NARROW_POOL_SIZE
) -> DatabaseSettings:
    return replace(
        settings,
        pool_size=pool_size,
        max_overflow=0,
        pool_timeout_seconds=NARROW_POOL_TIMEOUT_SECONDS,
    )


def narrow_database(database: Database, pool_size: int = NARROW_POOL_SIZE) -> Database:
    return Database(narrow_settings(database.settings, pool_size))


class ConnectionLedger:
    def __init__(self, database: Database) -> None:
        self.opened = 0
        self.closed = 0
        self.closed_while_checked_out = 0
        self.checkouts = 0
        self.outstanding = 0
        for engine in {database.writer_engine, database.reader_engine}:
            sync_engine = engine.sync_engine
            event.listen(sync_engine, "connect", self._connect)
            event.listen(sync_engine, "close", self._close)
            event.listen(sync_engine, "close_detached", self._close)
            event.listen(sync_engine, "checkout", self._checkout)
            event.listen(sync_engine, "checkin", self._checkin)

    def _connect(self, *arguments: object) -> None:
        self.opened += 1

    def _close(self, *arguments: object) -> None:
        self.closed += 1
        if self.outstanding > 0:
            self.closed_while_checked_out += 1

    def _checkout(self, *arguments: object) -> None:
        self.checkouts += 1
        self.outstanding += 1

    def _checkin(self, *arguments: object) -> None:
        self.outstanding -= 1


async def reached(
    condition: Callable[[], bool],
    deadline_seconds: float = CONDITION_DEADLINE_SECONDS,
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_seconds
    while not condition():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0)
    return True


async def until(condition: Callable[[], bool]) -> None:
    if not await reached(condition):
        raise TimeoutError("the condition was not met before the deadline")


def sqlite_companions(database: Database) -> list[str]:
    if database.dialect is not Dialect.SQLITE:
        return []
    path = database.settings.sqlite_path
    candidates = [Path(f"{path}{suffix}") for suffix in SQLITE_COMPANION_SUFFIXES]
    return [candidate.name for candidate in candidates if candidate.exists()]


async def hold_writer(
    database: Database, name: str, entered: asyncio.Event, release: asyncio.Event
) -> None:
    async with database.transaction() as connection:
        await Seeder(connection).namespace(name)
        entered.set()
        await asyncio.wait_for(release.wait(), CONDITION_DEADLINE_SECONDS)


async def hold_reader(
    database: Database, entered: asyncio.Event, release: asyncio.Event
) -> None:
    async with database.read_transaction() as connection:
        await connection.execute(select(namespace.c.id).limit(1))
        entered.set()
        await asyncio.wait_for(release.wait(), CONDITION_DEADLINE_SECONDS)


async def write(database: Database, name: str) -> None:
    async with database.transaction() as connection:
        await Seeder(connection).namespace(name)


def failure_messages(results: Sequence[object]) -> list[str]:
    return [
        str(result) if isinstance(result, DatabaseClosedError) else repr(result)
        for result in results
    ]


class ShutdownDuringClose:
    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings
        self.database = Database(narrow_settings(settings))
        self.ledger = ConnectionLedger(self.database)

    async def leave_a_close_pending(self, close_started: bool) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        asyncio.ensure_future(hold_writer(self.database, "outlived", entered, release))
        await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)
        asyncio.ensure_future(
            self.database.close(deadline_seconds=CONDITION_DEADLINE_SECONDS)
        )
        if close_started:
            await until(lambda: self.database.lifecycle is Lifecycle.CLOSING)

    async def names_written(self) -> list[str]:
        reopened = Database(self.settings)
        try:
            async with reopened.read_transaction() as connection:
                rows = (await connection.execute(select(namespace.c.name))).all()
        finally:
            await reopened.close()
        return sorted(str(row[0]) for row in rows)

    def lifecycle_after_shutdown(self) -> Lifecycle:
        return self.database.lifecycle
