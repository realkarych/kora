import asyncio
import sqlite3
from pathlib import Path
from threading import Event

import aiosqlite
import pytest

from kora.db.connection import DatabaseSnapshotFailure, DatabaseSnapshotRejected
from tests.db.closing import CONDITION_DEADLINE_SECONDS
from tests.db.native_read_support import NativeQuery, owned_reader
from tests.support.databases import copy_sqlite_template


@pytest.mark.asyncio
async def test_snapshot_repeated_cancellation_waits_for_native_guard_restoration(
    tmp_path: Path,
    sqlite_template: Path,
) -> None:
    loop = asyncio.get_running_loop()
    cancel_requested = Event()
    finish_restoration = Event()
    restoration_started = asyncio.Event()
    restoration_expired = Event()

    def observe(statement: str) -> None:
        if statement == "PRAGMA busy_timeout=5000" and cancel_requested.is_set():
            loop.call_soon_threadsafe(restoration_started.set)
            if not finish_restoration.wait(CONDITION_DEADLINE_SECONDS):
                restoration_expired.set()

    async with owned_reader(
        copy_sqlite_template(sqlite_template, tmp_path / "native-restoration.db")
    ) as reader:
        probe = NativeQuery(reader, observe)
        task = asyncio.create_task(probe.run(loop.time() + CONDITION_DEADLINE_SECONDS))
        await asyncio.wait_for(probe.started.wait(), CONDITION_DEADLINE_SECONDS)

        try:
            cancel_requested.set()
            task.cancel("native cancellation")
            await asyncio.wait_for(
                restoration_started.wait(), CONDITION_DEADLINE_SECONDS
            )
            assert probe.ledger.outstanding == reader.units_in_flight() == 1
            assert probe.ledger.closed == 0
            assert task.cancel("cancellation during restoration")
        finally:
            finish_restoration.set()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
        await probe.assert_reused()

        assert cancelled.value.args == ("native cancellation",)
        assert not restoration_expired.is_set()


@pytest.mark.asyncio
async def test_snapshot_progress_interrupts_native_work_without_asyncio_timer(
    tmp_path: Path,
    sqlite_template: Path,
) -> None:
    loop = asyncio.get_running_loop()
    native_settled = Event()
    sentinel_queued = asyncio.Event()

    def observe(statement: str) -> None:
        if statement == "SELECT 42":
            native_settled.set()

    async with owned_reader(
        copy_sqlite_template(sqlite_template, tmp_path / "native-progress.db")
    ) as reader:
        probe = NativeQuery(reader, observe)
        deadline = loop.time() + 1.0
        task = asyncio.create_task(probe.run(deadline))
        await asyncio.wait_for(probe.started.wait(), CONDITION_DEADLINE_SECONDS)
        driver = probe.driver
        assert isinstance(driver, aiosqlite.Connection)

        async def native_sentinel() -> None:
            sentinel_queued.set()
            try:
                async with driver.execute("SELECT 42") as cursor:
                    assert await cursor.fetchone() == (42,)
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode != sqlite3.SQLITE_INTERRUPT:
                    raise

        sentinel = asyncio.create_task(native_sentinel())
        await asyncio.wait_for(sentinel_queued.wait(), CONDITION_DEADLINE_SECONDS)

        assert loop.time() < deadline
        assert not native_settled.is_set()
        settled_without_timer = native_settled.wait(CONDITION_DEADLINE_SECONDS)
        with pytest.raises(DatabaseSnapshotRejected) as refused:
            await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
        await asyncio.wait_for(sentinel, CONDITION_DEADLINE_SECONDS)
        await probe.assert_reused()

        assert settled_without_timer
        assert refused.value.reason is DatabaseSnapshotFailure.DEADLINE
