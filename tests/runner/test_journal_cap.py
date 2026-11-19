import asyncio
from pathlib import Path

import pytest

from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import canonical_bytes
from kora.observability.redaction import SecretRegistry
from kora.protocols.harness import EVENT_ADAPTER
from kora.protocols.identity import instant_wire
from kora.runner.journal import BatchAppend, JournalAppendOutcome, RunningJournal
from kora.runner.sink import BATCH_RECORDS, QUEUE_RECORDS, RunnerSink
from tests.runner.agent_support import (
    HeldWriter,
    JournalProxy,
    SteppedClock,
    private_directory,
)
from tests.runner.test_agent_sink import journal_records, turn


def encoded_record(clock: SteppedClock, index: int, data: JsonObject) -> bytes:
    return (
        canonical_bytes(
            {"i": index, "at": instant_wire(clock.now()), "kind": "turn", "data": data}
        )
        + b"\n"
    )


def emitted_size(clock: SteppedClock, index: int, data: JsonObject) -> int:
    event = EVENT_ADAPTER.validate_python(
        {"i": index, "at": clock.now(), "kind": "turn", "data": data}
    )
    return len(canonical_bytes(event.model_dump(mode="json")))


@pytest.mark.parametrize("extra_bytes", [-1, 0, 1])
def test_capped_batch_writes_fitting_prefix(tmp_path: Path, extra_bytes: int) -> None:
    root = tmp_path / "attempt"
    attempt = private_directory(root)
    clock = SteppedClock()
    path = root / "journal.jsonl"
    prefix = (
        canonical_bytes(
            {
                "i": 0,
                "at": instant_wire(clock.now()),
                "kind": "runner.started",
                "data": {},
            }
        )
        + b"\n"
    )
    path.write_bytes(prefix)
    path.chmod(0o600)
    journal = RunningJournal(attempt, clock)
    assert (
        journal.append_outcome("runner.session_spec", {})
        is JournalAppendOutcome.DURABLE
    )
    initial = path.read_bytes()
    batch = [("turn", turn("x" * 512, f"m{i}")) for i in range(4)]
    lines = [encoded_record(clock, i + 1, data) for i, (_, data) in enumerate(batch)]
    cap = len(initial) + len(lines[0]) + len(lines[1]) + extra_bytes
    expected = 1 if extra_bytes < 0 else 2

    try:
        result = journal.append_batch(batch, cap_bytes=cap)
        stored = path.read_bytes()
        refused = journal.append_batch(batch[expected:], cap_bytes=len(stored))
        open_after_cap = not journal.closed
        following = journal.append_outcome("runner.finished", {})
        records = journal_records(root)
    finally:
        journal.close()
        attempt.close()

    assert result == BatchAppend(expected, JournalAppendOutcome.DURABLE)
    assert stored == initial + b"".join(lines[:expected])
    assert len(stored) <= cap
    assert refused == BatchAppend(0, None)
    assert open_after_cap
    assert following is JournalAppendOutcome.DURABLE
    assert records[-1]["i"] == expected + 1
    assert records[-1]["kind"] == "runner.finished"
    assert journal.not_durable == 0


@pytest.mark.asyncio
async def test_sink_counts_adapter_records_past_cap(tmp_path: Path) -> None:
    root = tmp_path / "attempt"
    attempt = private_directory(root)
    clock = SteppedClock()
    journal = RunningJournal(attempt, clock)
    assert (
        journal.append_outcome("runner.session_spec", {})
        is JournalAppendOutcome.DURABLE
    )
    writer = HeldWriter(journal=journal)
    cap = 4096
    sink = ObservableSink(
        writer, SecretRegistry(), clock, None, 900_000_000_000, journal_cap=cap
    )
    events = [
        turn("x" * 128, f"m{i}") for i in range(BATCH_RECORDS + QUEUE_RECORDS + 1)
    ]
    deadline = 10_000_000_000

    try:
        for data in events[:BATCH_RECORDS]:
            sink.emit("turn", data)
        assert await asyncio.to_thread(writer.entered.wait, 1)
        for data in events[BATCH_RECORDS:]:
            sink.emit("turn", data)
        assert sink.liveness().full_since_ns is not None
        writer.gate.set()
        assert await asyncio.to_thread(sink.wait_capped)
        assert sink.liveness().full_since_ns is None
        settling = asyncio.create_task(sink.settle(deadline))
        async with asyncio.timeout(1):
            complete = await settling
        repeated = await sink.settle(deadline)
        records = journal_records(root)
        raw_lines = (root / "journal.jsonl").read_bytes().splitlines(keepends=True)
    finally:
        writer.gate.set()
        sink.close()
        assert await sink.joined(deadline)
        journal.close()
        attempt.close()

    admitted = len(records) - 3
    assert 0 < admitted < BATCH_RECORDS
    assert sum(len(line) for line in raw_lines[:-2]) <= cap
    assert [record["kind"] for record in records] == ["runner.session_spec"] + [
        "turn"
    ] * admitted + ["runner.journal_gap", "runner.cap_reached"]
    assert [record["i"] for record in records] == list(range(len(records)))
    assert records[-2]["data"] == {"omitted": len(events) - admitted}
    assert records[-1]["data"] == {
        "kind": "journal",
        "omitted": sum(
            emitted_size(clock, i, events[i]) for i in range(admitted, len(events) - 1)
        ),
    }
    assert sink.omitted() == len(events) - admitted
    assert not complete
    assert not repeated
    assert sink.cause() is None
    assert sink.liveness().full_since_ns is None


class ObservableSink(RunnerSink):
    def wait_capped(self) -> bool:
        with self._state:
            return self._state.wait_for(lambda: self._capped, timeout=1)

    def queued(self) -> tuple[int, int]:
        with self._state:
            return len(self._queue), self._queued_bytes


@pytest.mark.asyncio
async def test_capped_sink_keeps_session_running(tmp_path: Path) -> None:
    root = tmp_path / "attempt"
    attempt = private_directory(root)
    clock = SteppedClock()
    journal = RunningJournal(attempt, clock)
    assert (
        journal.append_outcome("runner.session_spec", {})
        is JournalAppendOutcome.DURABLE
    )
    writer = HeldWriter(journal=journal)
    writer.gate.set()
    sink = ObservableSink(
        writer, SecretRegistry(), clock, None, 900_000_000_000, journal_cap=1024
    )
    stopped = asyncio.create_task(sink.termination())
    deadline = 10_000_000_000
    first = turn("x" * 2048)
    later = turn(message_id="late")
    expected_bytes = emitted_size(clock, 0, first)

    try:
        sink.emit("turn", first)
        await clock.advance_to(50_000_000)
        assert await asyncio.to_thread(sink.wait_capped)
        expected_bytes += sum(
            emitted_size(clock, index, later) for index in range(1, 10_001)
        )
        before = (root / "journal.jsonl").read_bytes()
        for _ in range(10_000):
            sink.emit("turn", later)
        after_emit = (root / "journal.jsonl").read_bytes()
        queued = sink.queued()
        complete = await sink.settle(deadline)
        repeated = await sink.settle(deadline)
        records = journal_records(root)
        unresolved = not stopped.done()
    finally:
        stopped.cancel()
        await asyncio.gather(stopped, return_exceptions=True)
        sink.close()
        assert await sink.joined(deadline)
        journal.close()
        attempt.close()

    assert not complete
    assert not repeated
    assert sink.omitted() == 10_001
    assert after_emit == before
    assert queued == (0, 0)
    assert len(writer.batches) == 1
    assert len(writer.appended) == 1
    assert [record["kind"] for record in records] == [
        "runner.session_spec",
        "runner.journal_gap",
        "runner.cap_reached",
    ]
    assert records[-2]["data"] == {"omitted": 10_001}
    assert records[-1]["data"] == {"kind": "journal", "omitted": expected_bytes}
    assert unresolved
    assert sink.cause() is None
    assert sink.liveness().full_since_ns is None


@pytest.mark.asyncio
async def test_failed_cap_record_halts_with_journal_unavailable(tmp_path: Path) -> None:
    attempt = private_directory(tmp_path / "attempt")
    clock = SteppedClock()
    journal = RunningJournal(attempt, clock)
    assert (
        journal.append_outcome("runner.session_spec", {})
        is JournalAppendOutcome.DURABLE
    )
    writer = JournalProxy(journal, on="runner.journal_gap", action="fail")
    sink = ObservableSink(
        writer, SecretRegistry(), clock, None, 900_000_000_000, journal_cap=1024
    )
    deadline = 10_000_000_000

    try:
        sink.emit("turn", turn("x" * 2048))
        await clock.advance_to(50_000_000)
        assert await asyncio.to_thread(sink.wait_capped)
        async with asyncio.timeout(1):
            complete = await sink.settle(deadline)
            terminated = await sink.termination()
    finally:
        sink.close()
        assert await sink.joined(deadline)
        journal.close()
        attempt.close()

    assert not complete
    assert sink.cause() == "journal_unavailable"
    assert terminated == "journal_unavailable"
