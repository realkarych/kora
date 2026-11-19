import time
import tracemalloc
from array import array
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from kora.protocols.time import SystemClock
from kora.runner.calls import TRACKING_CEILING
from kora.runner.journal import JournalAppendOutcome
from kora.runner.sink import BATCH_RECORDS, QUEUE_BYTES, QUEUE_RECORDS
from tests.measurements.harness import Sample, new_report, write_report
from tests.measurements.runner_sink_workload import (
    ObservedSink,
    RecordingWriter,
    SlowSyncJournal,
    Supervisor,
    call_events,
    journal_adapter_records,
    mixed_events,
    percentiles,
    resource_snapshot,
    small_events,
)
from tests.runner.agent_support import private_directory

WORKLOADS: tuple[tuple[str, int, float, int], ...] = (
    ("drain", 100_000, 0, 3),
    ("slow_sync_5ms", 20_000, 0.005, 3),
    ("slow_sync_50ms", 10_000, 0.05, 3),
    ("memory", TRACKING_CEILING + 1_000, 0, 1),
    ("saturation", 100_000, 0, 3),
)


def counter(sample: Sample, key: str) -> int:
    value = sample.extra[key]
    if type(value) is not int:
        raise ValueError(f"invalid counter: {key}")
    return value


def gates(samples: Sequence[Sample]) -> dict[str, bool]:
    return {
        "samples_complete": sorted((s.workload, s.repeat) for s in samples)
        == sorted(
            (name, repeat)
            for name, _, _, repeats in WORKLOADS
            for repeat in range(repeats)
        ),
        "batch_bound": all(counter(s, "batch_max") <= BATCH_RECORDS for s in samples),
        "queue_records_bound": all(
            counter(s, "peak_queue_records") <= QUEUE_RECORDS for s in samples
        ),
        "queue_bytes_bound": all(
            counter(s, "peak_queue_bytes") <= QUEUE_BYTES for s in samples
        ),
        "open_calls_bound": all(
            counter(s, "peak_open_calls") <= TRACKING_CEILING for s in samples
        ),
        "lossless": all(
            s.extra["omitted"] == 0 for s in samples if s.workload != "saturation"
        ),
        "saturation_accounting": all(
            counter(s, "omitted") + counter(s, "journal_adapter_records") == s.count
            for s in samples
            if s.workload == "saturation"
        ),
        "threads_released": all(
            s.extra["before_threads"] == s.extra["after_threads"] for s in samples
        ),
        "descriptors_released": all(
            s.extra["before_descriptors"] == s.extra["after_descriptors"]
            for s in samples
        ),
    }


async def measure(
    root: Path, workload: str, count: int, delay: float, repeat: int
) -> Sample:
    memory = workload == "memory"
    extra = resource_snapshot("before", traced=memory)
    directory = private_directory(root)
    journal = SlowSyncJournal(directory, delay)
    writer = RecordingWriter(journal)
    sink: ObservedSink | None = None
    supervisor: Supervisor | None = None
    emit_ns: array[int] = array("Q")
    started = time.perf_counter()
    try:
        assert (
            journal.append_outcome("runner.session_spec", {})
            is JournalAppendOutcome.DURABLE
        )
        sink = ObservedSink(writer)
        if delay:
            supervisor = Supervisor(journal)
            supervisor.thread.start()
        events = (
            call_events(count, opening=True)
            if memory
            else small_events(count)
            if workload == "saturation"
            else mixed_events(count)
        )
        for index, (kind, data) in enumerate(events, start=1):
            before = time.perf_counter_ns()
            sink.emit(kind, data)
            emit_ns.append(time.perf_counter_ns() - before)
            if workload != "saturation" and index % 1000 == 0:
                assert sink.wait_empty()
        extra.update(resource_snapshot("during", traced=memory))
        if memory:
            assert sink.open_calls() == TRACKING_CEILING
            for index, (kind, data) in enumerate(
                call_events(count, opening=False), start=1
            ):
                before = time.perf_counter_ns()
                sink.emit(kind, data)
                emit_ns.append(time.perf_counter_ns() - before)
                if index % 1000 == 0:
                    assert sink.wait_empty()
            assert sink.open_calls() == 0
        if supervisor is not None:
            supervisor.close()
        sink.close()
        joined = await sink.joined(SystemClock().monotonic_ns() + 5_000_000_000)
        assert joined
        sink.join_writer()
        extra.update(resource_snapshot("joined", traced=memory))
        elapsed = time.perf_counter() - started
        if workload == "saturation":
            extra["journal_adapter_records"] = journal_adapter_records(
                root / "journal.jsonl"
            )
        extra.update(
            {
                "omitted": sink.omitted(),
                "peak_queue_records": sink.peak_queue_records,
                "peak_queue_bytes": sink.peak_queue_bytes,
                "peak_open_calls": sink.peak_open_calls,
                "durable_records": writer.durable_records,
                "journal_bytes": (root / "journal.jsonl").stat().st_size,
                "batch_count": len(writer.batch_sizes),
                "batch_min": min(writer.batch_sizes, default=0),
                "batch_max": max(writer.batch_sizes, default=0),
                "supervisor_appends": 0 if supervisor is None else supervisor.appended,
            }
        )
    finally:
        if supervisor is not None:
            supervisor.stop.set()
        if sink is not None:
            sink.close()
        journal.close()
        try:
            if supervisor is not None:
                supervisor.thread.join(5)
                assert not supervisor.thread.is_alive()
            if sink is not None:
                assert await sink.joined(SystemClock().monotonic_ns() + 5_000_000_000)
                sink.join_writer()
        finally:
            journal.join_writer()
            directory.close()
            (root / "journal.jsonl").unlink(missing_ok=True)
            root.rmdir()
    extra.update(resource_snapshot("after", traced=memory))
    for prefix, values in (("emit", emit_ns), ("durable_latency", writer.latencies)):
        for suffix, value in zip(
            ("p50_ns", "p99_ns", "max_ns"), percentiles(values), strict=True
        ):
            extra[f"{prefix}_{suffix}"] = value
    extra["emit_total_seconds"] = sum(emit_ns) / 1_000_000_000
    extra["sync_delay_seconds"] = delay
    extra["events_per_second"] = len(emit_ns) / elapsed
    extra["paced"] = workload != "saturation"
    extra["observer_array_bytes"] = emit_ns.itemsize * len(
        emit_ns
    ) + writer.latencies.itemsize * len(writer.latencies)
    return Sample(workload, repeat, len(emit_ns), elapsed, extra)


@pytest.mark.measurement
@pytest.mark.asyncio
async def test_runner_sink_cost(tmp_path: Path) -> None:
    report = new_report()
    report.environment["method"] = (
        "Serial samples in one process, fresh real journal per sample; "
        "no warm-up; three repeats of drain (100000 events), slow_sync_5ms "
        "(20000), slow_sync_50ms (10000) and saturation (100000), and one "
        "memory repeat, sized to keep the daily step under two minutes; "
        "SystemClock batching; fixed-cycle mixed turn/tool_result text payloads of "
        "256, 512, 1024, 2048, 4096 bytes. Drain, both slow sync workloads and "
        "memory emit in 1000-event bursts, waiting for an empty queue with a "
        "5s deadline. Saturation emits 100000 events unpaced. "
        "Memory opens 101000 calls, of which the sink tracks the 100000-call "
        "ceiling, then emits 101000 results that close them. Saturation uses "
        "fixed 256-byte turn events so the 4096-record queue bound binds "
        "before the 4 MiB byte bound. Traced memory includes the observer's "
        "timing arrays (observer_array_bytes) and the open-call set's table, "
        "which keeps its capacity while the sink object lives. "
        "Emit timing uses perf_counter_ns; queued-to-durable uses monotonic_ns on "
        "both ends and excludes validation before enqueue. Latencies cover durable "
        "records only and describe 1000-event bursts on an empty queue; an "
        "isolated event waits up to the 50 ms batch window. The slow sync wait "
        "delays every journal sync, adapter batches included. "
        "Observer locks and timing arrays are included in costs. "
        "Tracemalloc enabled only for memory; RSS from proc statm, "
        "unavailable on macOS. "
        "Supervisor requests at 10ms intervals, serialized when sync exceeds period. "
        "Sample duration ends after sink join, before journal counting and cleanup. "
        "Timing, heap and RSS are diagnostic, with no numerical gates."
    )

    try:
        for name, count, delay, repeats in WORKLOADS:
            for repeat in range(repeats):
                if name == "memory":
                    tracemalloc.start()
                try:
                    report.samples.append(
                        await measure(
                            tmp_path / f"{name}-{repeat}", name, count, delay, repeat
                        )
                    )
                finally:
                    if name == "memory":
                        tracemalloc.stop()
        report.gates.update(gates(report.samples))
    finally:
        write_report(report)

    assert all(report.gates.values()), report.gates


def test_gates_refuse_broken_bounds() -> None:
    extra = {
        "batch_max": BATCH_RECORDS,
        "peak_queue_records": QUEUE_RECORDS,
        "peak_queue_bytes": QUEUE_BYTES,
        "peak_open_calls": TRACKING_CEILING,
        "omitted": 0,
        "before_threads": 1,
        "after_threads": 1,
        "before_descriptors": 4,
        "after_descriptors": 4,
    }
    samples = [
        Sample(name, repeat, count, 1, {**extra, "journal_adapter_records": count})
        for name, count, _, repeats in WORKLOADS
        for repeat in range(repeats)
    ]
    broken = [
        ("batch_max", BATCH_RECORDS + 1, "batch_bound"),
        ("peak_queue_records", QUEUE_RECORDS + 1, "queue_records_bound"),
        ("peak_queue_bytes", QUEUE_BYTES + 1, "queue_bytes_bound"),
        ("peak_open_calls", TRACKING_CEILING + 1, "open_calls_bound"),
        ("omitted", 1, "lossless"),
        ("after_threads", 2, "threads_released"),
        ("after_descriptors", 5, "descriptors_released"),
    ]

    accepted = gates(samples)
    refused = [
        (
            gate,
            gates(
                [
                    replace(samples[0], extra={**samples[0].extra, key: value}),
                    *samples[1:],
                ]
            ),
        )
        for key, value, gate in broken
    ]
    missing = gates(samples[:-1])
    duplicate = gates([*samples, samples[0]])
    saturation = samples[-1]
    accounted = replace(
        saturation,
        extra={
            **saturation.extra,
            "omitted": 7,
            "journal_adapter_records": saturation.count - 7,
        },
    )
    lost = replace(
        accounted,
        extra={**accounted.extra, "journal_adapter_records": saturation.count - 8},
    )
    balanced = gates([*samples[:-1], accounted])
    unbalanced = gates([*samples[:-1], lost])
    slow_loss = [
        gates(
            [
                replace(sample, extra={**sample.extra, "omitted": 1})
                if sample.workload == name
                else sample
                for sample in samples
            ]
        )
        for name in ("slow_sync_5ms", "slow_sync_50ms", "memory")
    ]

    assert all(accepted.values())
    assert all(not result[gate] for gate, result in refused)
    assert not missing["samples_complete"]
    assert not duplicate["samples_complete"]

    assert all(balanced.values())
    assert not unbalanced["saturation_accounting"]
    assert all(not result["lossless"] for result in slow_loss)


def test_accounting_reads_real_journal(tmp_path: Path) -> None:
    root = tmp_path / "accounting"
    directory = private_directory(root)
    journal = SlowSyncJournal(directory, 0)
    records = list(mixed_events(2))

    try:
        supervisor = journal.append_outcome("runner.measurement", {})
        appended = journal.append_batch(records, cap_bytes=1_048_576)
        count = journal_adapter_records(root / "journal.jsonl")
    finally:
        journal.close()
        journal.join_writer()
        directory.close()

    assert supervisor is JournalAppendOutcome.DURABLE
    assert appended.outcome is JournalAppendOutcome.DURABLE
    assert appended.admitted == count == 2
