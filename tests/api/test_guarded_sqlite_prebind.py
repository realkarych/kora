import asyncio
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.exc import OperationalError

from kora.db.connection import Database, DatabaseClosedError
from kora.db.run_event_page_v1 import SQLITE_EVENT_PAGE_INDEX
from tests.api.guarded_sqlite_prebind_child import (
    WITNESS_TIMEOUT,
    ReadObservations,
    check_schema,
    check_withdrawal,
    guarded_schema_ready,
    refused_writer,
)
from tests.support.databases import copy_sqlite_template


class ReadScope:
    def __init__(self) -> None:
        self._withdrawn = threading.Event()

    def require_live(self) -> None:
        if self._withdrawn.is_set():
            raise DatabaseClosedError("the test read scope was withdrawn")

    def withdraw(self) -> None:
        self._withdrawn.set()


def ordinary_image(source: Path, path: Path, *, inexact: bool = False) -> bytes:
    copy_sqlite_template(source, path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == (
            "delete",
        )
        if inexact:
            connection.execute(f"DROP INDEX {SQLITE_EVENT_PAGE_INDEX}").close()
    return path.read_bytes()


@pytest.mark.asyncio
async def test_guarded_sqlite_api_prebind_reads_and_retires_native_connections(
    tmp_path: Path, sqlite_template: Path
) -> None:
    path = tmp_path / "prepared main#.db"
    original = ordinary_image(sqlite_template, path)
    scope = ReadScope()
    database = Database.guarded_sqlite_reader(path, scope.require_live)
    observations = ReadObservations()
    observations.attach(database)

    await refused_writer(database)
    restored_schema = await check_schema(database, scope.require_live)

    assert restored_schema
    observations.assert_retired(2)
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_guarded_sqlite_api_prebind_refuses_unowned_or_inexact_input(
    tmp_path: Path, sqlite_template: Path
) -> None:
    ordinary = tmp_path / "ordinary.db"
    inexact = tmp_path / "inexact.db"
    missing = tmp_path / "missing.db"
    ordinary_before = ordinary_image(sqlite_template, ordinary)
    inexact_before = ordinary_image(sqlite_template, inexact, inexact=True)
    live = ReadScope()
    lost = ReadScope()
    lost.withdraw()
    unavailable = Database.guarded_sqlite_reader(ordinary, lost.require_live)
    broken = Database.guarded_sqlite_reader(inexact, live.require_live)
    absent = Database.guarded_sqlite_reader(missing, live.require_live)
    observations = ReadObservations()
    observations.attach(unavailable)
    observations.attach(absent)
    inexact_observations = ReadObservations()
    inexact_observations.attach(broken)
    withdrawing = Database.guarded_sqlite_reader(ordinary, live.require_live)
    withdrawal_observations = ReadObservations()
    withdrawal_observations.attach(withdrawing)

    with pytest.raises(DatabaseClosedError, match="withdrawn"):
        await check_schema(unavailable, lost.require_live)
    with pytest.raises(OperationalError, match="unable to open") as refusal:
        await check_schema(absent, live.require_live)
    restored_schema = await check_schema(broken, live.require_live)
    await check_withdrawal(withdrawing, live.withdraw, withdrawal_observations)

    assert not restored_schema
    assert isinstance(refusal.value.orig, sqlite3.OperationalError)
    observations.assert_retired(0)
    inexact_observations.assert_retired(2)
    assert set(tmp_path.iterdir()) == {ordinary, inexact}
    assert ordinary.read_bytes() == ordinary_before
    assert inexact.read_bytes() == inexact_before


@pytest.mark.asyncio
async def test_guarded_sqlite_api_prebind_refuses_withdrawal_at_disposal(
    tmp_path: Path, sqlite_template: Path
) -> None:
    path = tmp_path / "prepared.db"
    original = ordinary_image(sqlite_template, path)
    scope = ReadScope()
    database = Database.guarded_sqlite_reader(path, scope.require_live)
    observations = ReadObservations()
    observations.attach(database)

    def withdraw_before_publication(engine: Engine) -> None:
        scope.withdraw()

    event.listen(
        database.reader_engine.sync_engine,
        "engine_disposed",
        withdraw_before_publication,
    )

    with pytest.raises(DatabaseClosedError, match="withdrawn"):
        await check_schema(database, scope.require_live)

    observations.assert_retired(2)
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_guarded_sqlite_api_prebind_cancellation_retires_the_dispatched_open(
    tmp_path: Path, sqlite_template: Path
) -> None:
    path = tmp_path / "prepared.db"
    original = ordinary_image(sqlite_template, path)
    loop = asyncio.get_running_loop()
    event_loop_thread = threading.current_thread()
    entered = asyncio.Event()
    finish_open = threading.Event()
    observations = ReadObservations()

    def require_live() -> None:
        if threading.current_thread() is event_loop_thread:
            return
        loop.call_soon_threadsafe(entered.set)
        if not finish_open.wait(WITNESS_TIMEOUT):
            raise TimeoutError("the test did not release the original SQLite open")

    database = Database.guarded_sqlite_reader(path, require_live)
    observations.attach(database)

    async with asyncio.timeout(WITNESS_TIMEOUT):
        starting = asyncio.create_task(guarded_schema_ready(database))
        try:
            await entered.wait()
            starting.cancel()
            await asyncio.sleep(0)
            assert not starting.done()
        finally:
            finish_open.set()
            try:
                with pytest.raises(asyncio.CancelledError):
                    await starting
            finally:
                await database.close_sqlite_serving()

    observations.assert_retired(1)
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_bytes() == original
