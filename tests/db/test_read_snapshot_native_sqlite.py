import asyncio
from pathlib import Path
from threading import Event

import pytest

from tests.db.closing import CONDITION_DEADLINE_SECONDS
from tests.db.native_read_support import NativeQuery, owned_reader
from tests.support.databases import sqlite_settings


@pytest.mark.asyncio
async def test_snapshot_repeated_cancellation_waits_for_native_guard_restoration(
    tmp_path: Path,
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
        sqlite_settings(tmp_path / "native-restoration.db")
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
