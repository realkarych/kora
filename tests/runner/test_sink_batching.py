import asyncio
import threading
from pathlib import Path

import pytest

from kora.observability.redaction import SecretRegistry
from kora.protocols.time import SystemClock
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.journal import JournalAppendOutcome, RunningJournal, read_records
from kora.runner.sink import (
    BATCH_RECORDS,
    BATCH_WINDOW_NS,
    JOURNAL_CAP_BYTES,
    QUEUE_RECORDS,
    RunnerSink,
)
from tests.runner.agent_support import (
    HeldWriter,
    SteppedClock,
    drained,
    private_directory,
)
from tests.runner.test_agent_sink import journal_records, turn


def test_append_batch_writes_consecutive_records_with_one_outcome(
    tmp_path: Path,
) -> None:
    attempt = private_directory(tmp_path / "attempt")
    journal = RunningJournal(attempt, SystemClock())
    assert journal.append_outcome("runner.started", {}) is JournalAppendOutcome.DURABLE
    batch = [("turn", turn(message_id=f"m{index}")) for index in range(3)]

    try:
        outcome = journal.append_batch(batch, cap_bytes=JOURNAL_CAP_BYTES)
        records = read_records(attempt)
        with pytest.raises(ValueError, match="must not be empty"):
            journal.append_batch([], cap_bytes=JOURNAL_CAP_BYTES)
        following = journal.append_outcome("runner.finished", {})
        journal.close()
        rejected = journal.append_batch(batch, cap_bytes=JOURNAL_CAP_BYTES)
        final = read_records(attempt)
    finally:
        journal.close()
        attempt.close()

    assert outcome.admitted == 3
    assert outcome.outcome is JournalAppendOutcome.DURABLE
    assert records == ((0, "runner.started"), (1, "turn"), (2, "turn"), (3, "turn"))
    assert following is JournalAppendOutcome.DURABLE
    assert rejected.outcome is JournalAppendOutcome.ABSENT
    assert final == (*records, (4, "runner.finished"))
    assert journal.not_durable == 0


class HeldBatchWrite(RunningJournal):
    def __init__(self, directory: DirectoryAnchor) -> None:
        super().__init__(directory, SystemClock())
        self.entered = threading.Event()
        self.release = threading.Event()

    def _write_line(self, descriptor: int, line: bytes, deadline: int) -> bool:
        stored = super()._write_line(descriptor, line, deadline)
        if stored and line.count(b"\n") > 1:
            self.entered.set()
            self.release.wait(10)
        return stored


@pytest.mark.asyncio
async def test_uncertain_batch_counts_every_record_not_durable(
    tmp_path: Path,
) -> None:
    attempt = private_directory(tmp_path / "attempt")
    journal = HeldBatchWrite(attempt)
    batch = [("turn", turn(message_id=f"m{index}")) for index in range(3)]

    try:
        writing = asyncio.create_task(
            asyncio.to_thread(journal.append_batch, batch, cap_bytes=JOURNAL_CAP_BYTES)
        )
        entered = await asyncio.to_thread(journal.entered.wait, 10)
        journal.close()
        async with asyncio.timeout(10):
            outcome = await asyncio.shield(writing)
        not_durable = journal.not_durable
    finally:
        journal.close()
        journal.release.set()
        attempt.close()

    assert entered
    assert outcome.outcome is JournalAppendOutcome.UNCERTAIN
    assert not_durable == len(batch)


@pytest.mark.asyncio
async def test_writer_flushes_at_256_records() -> None:
    writer = HeldWriter()
    sink = RunnerSink(writer, SecretRegistry(), SteppedClock(), None, 900_000_000_000)

    try:
        for index in range(BATCH_RECORDS):
            sink.emit("turn", turn(message_id=f"m{index}"))
        assert await asyncio.to_thread(writer.entered.wait, 10)
        for index in range(BATCH_RECORDS, 600):
            sink.emit("turn", turn(message_id=f"m{index}"))
        writer.gate.set()
        sink.close()
        joined = await sink.joined(10_000_000_000)
    finally:
        writer.gate.set()
        sink.close()
        await sink.joined(10_000_000_000)

    assert joined
    assert [len(batch) for batch in writer.batches] == [256, 256, 88]
    assert [data["message_id"] for _, data in writer.appended] == [
        f"m{i}" for i in range(600)
    ]
    assert sink.omitted() == 0


@pytest.mark.asyncio
async def test_writer_flushes_after_50_ms() -> None:
    clock = SteppedClock()
    writer = HeldWriter()
    sink = RunnerSink(writer, SecretRegistry(), clock, None, 900_000_000_000)
    writer.gate.set()

    try:
        sink.emit("turn", turn(message_id="m0"))
        await clock.advance_to(BATCH_WINDOW_NS - 1)
        sink.emit("turn", turn(message_id="m1"))
        await clock.advance_to(BATCH_WINDOW_NS)
        reached = await asyncio.to_thread(writer.entered.wait, 10)
        sink.emit("turn", turn(message_id="m2"))
        await clock.advance_to(2 * BATCH_WINDOW_NS)
        complete = await drained(sink)
    finally:
        sink.close()
        await sink.joined(clock.current + 10_000_000_000)

    assert reached
    assert [len(batch) for batch in writer.batches] == [2, 1]
    assert complete == (True, True)


@pytest.mark.asyncio
async def test_writer_keeps_late_queued_records_out_of_an_expired_batch() -> None:
    clock = SteppedClock()
    writer = HeldWriter()
    sink = RunnerSink(writer, SecretRegistry(), clock, None, 900_000_000_000)
    for index in range(BATCH_RECORDS):
        sink.emit("turn", turn(message_id=f"held{index}"))

    try:
        entered = await asyncio.to_thread(writer.entered.wait, 10)
        sink.emit("turn", turn(message_id="first"))
        await clock.advance_to(BATCH_WINDOW_NS - 1)
        sink.emit("turn", turn(message_id="inside"))
        await clock.advance_to(BATCH_WINDOW_NS)
        sink.emit("turn", turn(message_id="cutoff"))
        sink.close()
        writer.gate.set()
        joined = await sink.joined(clock.current + 10_000_000_000)
    finally:
        sink.close()
        writer.gate.set()
        await sink.joined(clock.current + 10_000_000_000)

    assert entered
    assert joined
    assert [len(batch) for batch in writer.batches] == [BATCH_RECORDS, 2, 1]
    assert [
        tuple(data["message_id"] for _, data in batch) for batch in writer.batches[1:]
    ] == [("first", "inside"), ("cutoff",)]
    assert sink.omitted() == 0


@pytest.mark.asyncio
async def test_supervisor_record_flushes_batch_first(tmp_path: Path) -> None:
    attempt = private_directory(tmp_path / "attempt")
    journal = RunningJournal(attempt, SystemClock())
    writer = HeldWriter(journal=journal)
    sink = RunnerSink(writer, SecretRegistry(), SteppedClock(), None, 900_000_000_000)

    try:
        for index in range(BATCH_RECORDS):
            sink.emit("turn", turn(message_id=f"m{index}"))
        assert await asyncio.to_thread(writer.entered.wait, 10)
        for index in range(QUEUE_RECORDS + 1):
            sink.emit("turn", turn(message_id=f"q{index}"))
        writer.gate.set()
        await sink.settle(10_000_000_000)
        after_gap = read_records(attempt)
        for _ in range(101):
            sink.emit("error", {"code": "harness_failed", "truncated": False})
        await sink.settle(10_000_000_000)
        after_cap = read_records(attempt)
        sink.emit("turn", turn(message_id="last"))
        complete = await drained(sink)
        records = journal_records(tmp_path / "attempt")
    finally:
        writer.gate.set()
        sink.close()
        await sink.joined(10_000_000_000)
        journal.close()
        attempt.close()

    admitted = BATCH_RECORDS + QUEUE_RECORDS
    assert [kind for _, kind in after_gap] == ["turn"] * admitted + [
        "runner.journal_gap"
    ]
    assert [kind for _, kind in after_cap] == [kind for _, kind in after_gap] + [
        "error"
    ] * 100 + ["runner.cap_reached"]
    assert [record["i"] for record in records] == list(range(len(records)))
    assert records[admitted]["data"] == {"omitted": 1}
    assert records[admitted + 101]["data"] == {"kind": "error_events", "omitted": 1}
    assert records[-2]["kind"] == "turn"
    assert records[-2]["data"] == {
        "message_id": "last",
        "agent_id": None,
        "text": None,
        "truncated": False,
    }
    assert complete == (False, True)


@pytest.mark.parametrize(
    "outcome",
    [JournalAppendOutcome.ABSENT, JournalAppendOutcome.UNCERTAIN],
    ids=["absent", "uncertain"],
)
@pytest.mark.asyncio
async def test_failed_batch_halts_and_counts_every_record(
    outcome: JournalAppendOutcome,
) -> None:
    writer = HeldWriter(outcome)
    sink = RunnerSink(writer, SecretRegistry(), SteppedClock(), None, 900_000_000_000)

    try:
        for index in range(BATCH_RECORDS):
            sink.emit("turn", turn(message_id=f"m{index}"))
        assert await asyncio.to_thread(writer.entered.wait, 10)
        for index in range(BATCH_RECORDS, 600):
            sink.emit("turn", turn(message_id=f"m{index}"))
        writer.gate.set()
        cause = await sink.termination()
        omitted = sink.omitted()
        sink.emit("turn", turn(message_id="late"))
        complete = await drained(sink)
    finally:
        writer.gate.set()
        sink.close()
        await sink.joined(10_000_000_000)

    assert cause == "journal_unavailable"
    assert sink.enforcement().cause == "journal_unavailable"
    assert omitted == 600
    assert sink.omitted() == 601
    assert [len(batch) for batch in writer.batches] == [256]
    assert len(writer.appended) == 256
    assert complete == (False, True)
