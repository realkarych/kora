from contextlib import closing
from pathlib import Path

import pytest

from kora.conformance.capture import Pipe
from kora.db import CUSTODY_REFUSED, DB_COMPONENT
from kora.db.custody_models import CustodyUnavailable
from kora.db.custody_native import CustodyLatch, CustodyNativeDirectory
from kora.db.custody_recovery import CustodyRecoveryPriority
from kora.observability import Level, LoggingSettings, configure_process
from kora.standalone.custody import NativeCustodyPlatform
from tests.db.closing import until
from tests.db.test_custody_native import journal_path
from tests.db.test_custody_recovery_priority import (
    GENERATION,
    JOURNAL_ID,
    initialize,
    register,
)
from tests.support.observability import FakeClock


@pytest.mark.asyncio
async def test_custody_diagnostics_exclude_content_and_unconfirmed_outcomes(
    tmp_path: Path,
) -> None:
    path = journal_path(tmp_path)
    pipe = Pipe.open()
    diagnostics = configure_process(
        DB_COMPONENT,
        LoggingSettings(level=Level.DEBUG),
        "custody-test",
        FakeClock().now,
        pipe.write_end,
    )
    pipe.reader.start()
    try:
        with closing(
            CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
        ) as root:
            with closing(CustodyRecoveryPriority(root)) as priority:
                await initialize(root)
                sidecar = path / "authority.sqlite3-wal"
                sidecar.touch(mode=0o600)

                with pytest.raises(CustodyUnavailable):
                    await register(priority)
                sidecar.unlink()
                await register(priority)
                await priority.request()
                await priority.withdraw()
        with closing(
            CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=False)
        ) as attached:
            with closing(CustodyRecoveryPriority(attached)) as successor:
                with pytest.raises(CustodyUnavailable):
                    await successor.succeed_native_only(
                        journal_id=JOURNAL_ID, generation=GENERATION
                    )
                refused_latch = attached.read_latch()
        assert diagnostics.shutdown()
        pipe.close_write_end()
        pipe.reader.wait_closed()
        records = pipe.reader.records()
        data = pipe.reader.data
    finally:
        diagnostics.shutdown()
        pipe.release()

    assert [
        record["state"]
        for record in records
        if record["event"] == "db.custody.transition"
    ] == [
        "ordinary_open",
        "hint_published",
        "native_turn",
        "ordinary_open",
        "successor_denial_persisted",
    ]
    assert refused_latch is CustodyLatch.BOOTSTRAP_FROM_OPEN
    assert [
        record["state"] for record in records if record["event"] == "db.custody.refused"
    ] == ["coordinator_operation", "coordinator_operation"]
    assert str(path).encode() not in data
    assert b"authority.sqlite3" not in data
    assert b"execution custody is unavailable" not in data


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_custody_releases_while_diagnostics_sink_is_unavailable(
    tmp_path: Path,
    broken: bool,
) -> None:
    path = journal_path(tmp_path)
    pipe = Pipe.open()
    diagnostics = configure_process(
        DB_COMPONENT,
        LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536),
        "custody-test",
        FakeClock().now,
        pipe.write_end,
    )
    if broken:
        pipe.close_read_end()
    log = diagnostics.logger(DB_COMPONENT)
    try:
        for _ in range(2048):
            log.emit(CUSTODY_REFUSED, state="ordinary_admission")
        if broken:
            await until(lambda: diagnostics.snapshot().sink_failed)
        with closing(
            CustodyNativeDirectory(path, NativeCustodyPlatform(), provision=True)
        ) as root:
            with closing(CustodyRecoveryPriority(root)) as priority:
                await initialize(root)
                await register(priority)

                await priority.request()
                await priority.withdraw()
            with (
                root.acquire("commit.lock"),
                root.acquire("journal-order.lock"),
                root.acquire("recovery.lock"),
            ):
                reopened = root.read_latch()
        dropped = diagnostics.snapshot().dropped_total()
    finally:
        if not broken:
            pipe.reader.start()
        assert diagnostics.shutdown()
        pipe.release()

    assert reopened is CustodyLatch.OPEN
    assert dropped > 0
