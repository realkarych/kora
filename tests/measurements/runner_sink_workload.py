import gc
import threading
import time
import tracemalloc
from array import array
from collections.abc import Iterator, Sequence
from itertools import cycle
from pathlib import Path

from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import decode_strict_json
from kora.observability.redaction import SecretRegistry
from kora.protocols.time import SystemClock
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.journal import BatchAppend, JournalAppendOutcome, RunningJournal
from kora.runner.sink import ADAPTER_KINDS, QUEUE_RECORDS, Record, RunnerSink
from tests.measurements.harness import ExtraValue, current_rss_bytes, open_descriptors


class SlowSyncJournal(RunningJournal):
    def __init__(self, directory: DirectoryAnchor, delay: float) -> None:
        super().__init__(directory, SystemClock())
        self.delay = delay
        self.delay_event = threading.Event()

    def _write_line(self, descriptor: int, line: bytes, deadline: int) -> bool:
        written = super()._write_line(descriptor, line, deadline)
        if written and self.delay:
            self.delay_event.wait(self.delay)
        return written

    def _close_locked(self) -> None:
        self.delay_event.set()
        super()._close_locked()

    def join_writer(self) -> None:
        if self._thread is not None:
            self._thread.join(5)
            assert not self._thread.is_alive()


class RecordingWriter:
    def __init__(self, journal: RunningJournal) -> None:
        self.journal = journal
        self.batch: Sequence[Record] = ()
        self.latencies: array[int] = array("Q")
        self.batch_sizes: array[int] = array("Q")
        self.durable_records = 0

    def append_batch(
        self, records: Sequence[tuple[str, JsonObject]], *, cap_bytes: int
    ) -> BatchAppend:
        result = self.journal.append_batch(records, cap_bytes=cap_bytes)
        durable_ns = time.monotonic_ns()
        self.batch_sizes.append(len(records))
        if result.outcome is JournalAppendOutcome.DURABLE:
            self.durable_records += result.admitted
            self.latencies.extend(
                durable_ns - record.queued_ns
                for record in self.batch[: result.admitted]
            )
        self.batch = ()
        return result

    def append_outcome(self, kind: str, data: JsonObject) -> JournalAppendOutcome:
        return self.journal.append_outcome(kind, data)


class ObservedSink(RunnerSink):
    def __init__(self, writer: RecordingWriter) -> None:
        self.writer = writer
        self.peak_queue_records = 0
        self.peak_queue_bytes = 0
        self.peak_open_calls = 0
        super().__init__(writer, SecretRegistry(), SystemClock(), None, 900_000_000_000)

    def _enqueue(self, record: Record) -> None:
        with self._state:
            super()._enqueue(record)
            records = len(self._queue)
            if records > QUEUE_RECORDS:
                records = sum(isinstance(item, Record) for item in self._queue)
            self.peak_queue_records = max(self.peak_queue_records, records)
            self.peak_queue_bytes = max(self.peak_queue_bytes, self._queued_bytes)
            self.peak_open_calls = max(self.peak_open_calls, len(self._open_calls))

    def _append_batch(self, batch: Sequence[Record]) -> bool:
        self.writer.batch = batch
        try:
            return super()._append_batch(batch)
        finally:
            with self._state:
                self._state.notify_all()

    def wait_empty(self) -> bool:
        with self._state:
            return self._state.wait_for(lambda: not self._queue, timeout=5)

    def open_calls(self) -> int:
        with self._state:
            return len(self._open_calls)

    def join_writer(self) -> None:
        self._thread.join(5)
        assert not self._thread.is_alive()


class Supervisor:
    def __init__(self, journal: RunningJournal) -> None:
        self.journal = journal
        self.stop = threading.Event()
        self.appended = 0
        self.failed = False
        self.thread = threading.Thread(target=self.run, name="measurement-supervisor")

    def run(self) -> None:
        due = time.monotonic() + 0.01
        while not self.stop.wait(max(0, due - time.monotonic())):
            outcome = self.journal.append_outcome("runner.measurement", {})
            if outcome is not JournalAppendOutcome.DURABLE:
                self.failed = True
                return
            self.appended += 1
            due = max(due + 0.01, time.monotonic())

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)
        assert not self.thread.is_alive()
        assert not self.failed


def mixed_events(count: int) -> Iterator[tuple[str, JsonObject]]:
    sizes = cycle((256, 512, 1024, 2048, 4096))
    for index in range(count):
        text = "x" * next(sizes)
        if index % 2:
            yield "turn", {"message_id": f"m{index}", "text": text, "truncated": False}
        else:
            yield (
                "tool_result",
                {
                    "tool_use_id": f"t{index}",
                    "content": text,
                    "is_error": False,
                    "truncated": False,
                },
            )


def small_events(count: int) -> Iterator[tuple[str, JsonObject]]:
    text = "x" * 256
    for index in range(count):
        yield "turn", {"message_id": f"m{index}", "text": text, "truncated": False}


def call_events(count: int, *, opening: bool) -> Iterator[tuple[str, JsonObject]]:
    for index in range(count):
        if opening:
            yield (
                "tool_call",
                {
                    "tool": "Read",
                    "tool_use_id": f"t{index}",
                    "output_channel": False,
                },
            )
        else:
            yield (
                "tool_result",
                {
                    "tool_use_id": f"t{index}",
                    "is_error": False,
                    "truncated": False,
                },
            )


def resource_snapshot(prefix: str, *, traced: bool) -> dict[str, ExtraValue]:
    result: dict[str, ExtraValue] = {
        f"{prefix}_threads": threading.active_count(),
        f"{prefix}_descriptors": open_descriptors(),
        f"{prefix}_rss_bytes": current_rss_bytes(),
    }
    if traced:
        gc.collect()
        settled, peak = tracemalloc.get_traced_memory()
        result[f"{prefix}_settled_traced_bytes"] = settled
        result[f"{prefix}_peak_traced_bytes"] = peak
    return result


def percentiles(values: Sequence[int]) -> tuple[int, int, int]:
    ordered = sorted(values)
    if not ordered:
        return 0, 0, 0
    return (
        ordered[(len(ordered) - 1) // 2],
        ordered[(len(ordered) * 99 - 1) // 100],
        ordered[-1],
    )


def journal_adapter_records(path: Path) -> int:
    count = 0
    with path.open("rb") as journal:
        for line in journal:
            record = decode_strict_json(line, 65_536, 8)
            assert isinstance(record, dict)
            kind = record["kind"]
            if isinstance(kind, str) and kind in ADAPTER_KINDS:
                count += 1
    return count
