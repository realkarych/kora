import gc
import hashlib
import logging
import os
import string
import subprocess
import sys
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator

import pytest

from kora.observability import (
    CATALOG,
    EventSpec,
    FieldKind,
    Level,
    LoggingSettings,
    ProcessDiagnostics,
    RenderFormat,
    bound_scope,
    configure_process,
)
from kora.observability.redaction import SecretRegistry, StreamMasker
from tests.measurements.harness import (
    Report,
    Sample,
    current_rss_bytes,
    report_lifecycle,
    settled_counts,
    timed,
    utc_now,
)

pytestmark = pytest.mark.measurement


@pytest.fixture
def report() -> Iterator[Report]:
    yield from report_lifecycle()


MEASUREMENT_COMPONENT: str = CATALOG.register_component("measurement")
SIXTEEN_FIELDS: dict[str, FieldKind] = {
    f"f{i:02d}": FieldKind.STRING for i in range(16)
}
WIDE: EventSpec = CATALOG.register(
    EventSpec(
        "measurement.wide", MEASUREMENT_COMPONENT, Level.INFO, "wide", SIXTEEN_FIELDS
    )
)
WIDE_DEBUG: EventSpec = CATALOG.register(
    EventSpec(
        "measurement.wide_debug",
        MEASUREMENT_COMPONENT,
        Level.DEBUG,
        "wide",
        SIXTEEN_FIELDS,
    )
)
MAXIMAL: EventSpec = CATALOG.register(
    EventSpec(
        "measurement.maximal",
        MEASUREMENT_COMPONENT,
        Level.INFO,
        "maximal",
        {
            **{f"s{i:02d}": FieldKind.STRING for i in range(12)},
            "exception": FieldKind.EXCEPTION,
        },
    )
)
NESTED: EventSpec = CATALOG.register(
    EventSpec(
        "measurement.nested",
        MEASUREMENT_COMPONENT,
        Level.INFO,
        "nested",
        {"detail": FieldKind.MAPPING, "text": FieldKind.STRING},
    )
)

RECORDS: int = 50_000
HEAVY_RECORDS: int = 1_000
BRIDGED_RECORDS: int = 10_000
REPEATS: int = 3
HEAVY_REPEATS: int = 2
CYCLES: int = 200
PRODUCER_THREADS: int = 4
STEP_BUDGET_SECONDS: float = 300.0
ROTATION_ROUNDS: int = 100
MATCHER_RECORDS: int = 200
MASKER_LINES: int = 100_000
MASKER_LINE: bytes = b"x" * 199 + b"\n"
MASKER_CHUNK_BYTES: int = 65_536
MASKER_VALUE_COUNTS: tuple[int, ...] = (0, 16, 256)
ADVERSARIAL_LINES: int = 10_000
ADVERSARIAL_VALUE: str = "a" * 4095 + "c"
ADVERSARIAL_LINE: bytes = b"a" * 199 + b"b"
MASKER_BUDGET_SECONDS: float = 60.0
SMALL_FEEDS: int = 4_000
RUN_FORMS: int = 256
NEAR_MISS_FORMS: int = 16
NEAR_MISS_FEEDS: int = 64
EXIT_MARGIN_SECONDS: float = 2.0
IDLE_SHUTDOWN_SECONDS: float = 0.5
HEAP_GROWTH_ALLOWANCE_BYTES: int = 1_048_576
HEAP_SLOPE_ALLOWANCE_BYTES: int = 65_536
RSS_GROWTH_ALLOWANCE_BYTES: int = 16_777_216

FULL_PIPE_SCRIPT: str = """
import datetime
from kora.observability import CATALOG, EventSpec, FieldKind, Level, LoggingSettings
from kora.observability import configure_process
component = CATALOG.register_component("filler")
spec = CATALOG.register(
    EventSpec("filler.note", component, Level.INFO, "fill", {"text": FieldKind.STRING})
)
diagnostics = configure_process(
    component, LoggingSettings(), "filler", lambda: datetime.datetime.now(datetime.UTC)
)
log = diagnostics.logger(component)
import sys, time
for index in range(3000):
    log.emit(spec, text="y" * 500)
started = time.monotonic()
diagnostics.shutdown()
sys.stdout.write("%.3f" % (time.monotonic() - started))
"""


class Sink:
    def __init__(self) -> None:
        self.read_end, self.write_end = os.pipe()
        self._thread: threading.Thread | None = None
        self.consumed = 0

    def drain(self) -> None:
        def run() -> None:
            while True:
                data = os.read(self.read_end, 65_536)
                if not data:
                    return
                self.consumed += len(data)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def close(self) -> None:
        os.close(self.write_end)
        if self._thread is not None:
            self._thread.join(5)
        os.close(self.read_end)


def configured(settings: LoggingSettings, sink: Sink) -> ProcessDiagnostics:
    return configure_process(
        MEASUREMENT_COMPONENT, settings, "measurement", utc_now, sink.write_end
    )


def repeat(
    report: Report,
    workload: str,
    count: int,
    repeats: int,
    action: Callable[[], None],
) -> None:
    action()
    for index in range(repeats):
        seconds = timed(action)
        report.samples.append(Sample(workload, index, count, seconds))


def wide_fields() -> dict[str, str]:
    return {f"f{i:02d}": f"value-{i:02d}-" + "x" * 40 for i in range(16)}


def maximal_fields(string_bytes: int) -> dict[str, object]:
    try:
        raise ValueError("maximal")
    except ValueError as error:
        exception: BaseException = error
    return {
        **{f"s{i:02d}": "m" * string_bytes for i in range(12)},
        "exception": exception,
    }


def adversarial_mapping() -> dict[str, object]:
    return {f"k{i:02d}": {f"n{j:02d}": "v" * 8 for j in range(64)} for i in range(64)}


def registered_values(count: int) -> list[str]:
    return [
        f"registered-value-{index:04d}-" + "r" * (index % 200) for index in range(count)
    ]


def scattered_registry_values(count: int, length: int) -> list[str]:
    alphabet = string.ascii_lowercase + string.digits
    values: list[str] = []
    for index in range(count):
        digest = b""
        while len(digest) < length:
            digest += hashlib.sha256(
                f"registry-{index}-{len(digest)}".encode()
            ).digest()
        values.append(
            "".join(alphabet[byte % len(alphabet)] for byte in digest[:length])
        )
    return values


def diverse_text(length: int, seed: int) -> str:
    words = [
        hashlib.sha256(f"word-{seed}-{index}".encode()).hexdigest()[: 2 + index % 8]
        for index in range(400)
    ]
    return " ".join(words)[:length]


def near_miss_values() -> list[str]:
    return [
        family + "x" * length for family in ("ab", "cb") for length in range(6, 133)
    ]


def test_logging_overhead_and_resource_lifetime(report: Report) -> None:
    step_started = time.perf_counter()

    producer_workloads(report)
    redaction_workloads(report)
    stream_masker_workloads(report)
    bridge_and_context_workloads(report)
    saturation_workloads(report)
    report.gates["process_exit_bounded"] = exit_on_full_pipe(report)
    report.gates["idle_shutdown_bounded"] = idle_shutdown(report)
    lifetime_gates = configure_shutdown_cycles(report)
    report.gates.update(lifetime_gates)
    cold_start(report)
    report.durations["step_total"] = time.perf_counter() - step_started

    assert report.durations["step_total"] < STEP_BUDGET_SECONDS
    assert all(report.gates.values()), report.gates


def producer_workloads(report: Report) -> None:
    sink = Sink()
    sink.drain()
    diagnostics = configured(LoggingSettings(level=Level.DEBUG), sink)
    disabled = diagnostics.logger(MEASUREMENT_COMPONENT, threshold=Level.WARNING)
    enabled = diagnostics.logger(MEASUREMENT_COMPONENT)
    fields = wide_fields()
    started = time.perf_counter()

    def emit_disabled() -> None:
        for _ in range(RECORDS):
            disabled.emit(WIDE, **fields)

    def emit_info() -> None:
        for _ in range(RECORDS):
            enabled.emit(WIDE, **fields)

    def emit_debug() -> None:
        for _ in range(RECORDS):
            enabled.emit(WIDE_DEBUG, **fields)

    repeat(report, "disabled_level", RECORDS, REPEATS, emit_disabled)
    repeat(report, "info_16_fields", RECORDS, REPEATS, emit_info)
    repeat(report, "debug_16_fields", RECORDS, REPEATS, emit_debug)
    maximal = maximal_fields(1024)

    def emit_maximal() -> None:
        for _ in range(HEAVY_RECORDS):
            enabled.emit(MAXIMAL, **maximal)

    repeat(report, "maximal_default_limit", HEAVY_RECORDS, HEAVY_REPEATS, emit_maximal)
    nested = adversarial_mapping()

    def emit_adversarial() -> None:
        for _ in range(HEAVY_RECORDS):
            enabled.emit(NESTED, detail=nested)

    def emit_oversized() -> None:
        for _ in range(HEAVY_RECORDS):
            enabled.emit(NESTED, text="o" * 65_536)

    repeat(
        report,
        "adversarial_nested_rejected",
        HEAVY_RECORDS,
        HEAVY_REPEATS,
        emit_adversarial,
    )
    repeat(
        report,
        "oversized_string_rejected",
        HEAVY_RECORDS,
        HEAVY_REPEATS,
        emit_oversized,
    )
    diagnostics.shutdown()
    sink.close()
    text_sink = Sink()
    text_sink.drain()
    text_diagnostics = configured(
        LoggingSettings(render_format=RenderFormat.TEXT), text_sink
    )
    text_logger = text_diagnostics.logger(MEASUREMENT_COMPONENT)

    def emit_text() -> None:
        for _ in range(RECORDS):
            text_logger.emit(WIDE, **fields)

    repeat(report, "text_renderer", RECORDS, REPEATS, emit_text)
    text_diagnostics.shutdown()
    text_sink.close()
    big_sink = Sink()
    big_sink.drain()
    big_settings = LoggingSettings(max_record_bytes=1_048_576, queue_bytes=16_777_216)
    big_diagnostics = configured(big_settings, big_sink)
    big_logger = big_diagnostics.logger(MEASUREMENT_COMPONENT)
    big_fields = maximal_fields(1024)

    def emit_big() -> None:
        for _ in range(HEAVY_RECORDS):
            big_logger.emit(MAXIMAL, **big_fields)

    repeat(report, "maximal_megabyte_limit", HEAVY_RECORDS, HEAVY_REPEATS, emit_big)
    big_diagnostics.shutdown()
    big_sink.close()
    report.durations["producer_workloads"] = time.perf_counter() - started


def redaction_workloads(report: Report) -> None:
    sink = Sink()
    sink.drain()
    diagnostics = configured(LoggingSettings(), sink)
    log = diagnostics.logger(MEASUREMENT_COMPONENT)
    started = time.perf_counter()
    registrations = [
        diagnostics.register_secret(value) for value in registered_values(256)
    ]
    miss = maximal_fields(1024)
    values = registered_values(256)
    hits = {
        **{
            f"s{i:02d}": " ".join(values[i * 20 : i * 20 + 20])[:1024]
            for i in range(12)
        },
        "exception": miss["exception"],
    }

    def emit_miss() -> None:
        for _ in range(HEAVY_RECORDS):
            log.emit(MAXIMAL, **miss)

    def emit_hits() -> None:
        for _ in range(HEAVY_RECORDS):
            log.emit(MAXIMAL, **hits)

    repeat(
        report,
        "redaction_maximal_registry_miss",
        HEAVY_RECORDS,
        HEAVY_REPEATS,
        emit_miss,
    )
    repeat(
        report,
        "redaction_maximal_registry_hits",
        HEAVY_RECORDS,
        HEAVY_REPEATS,
        emit_hits,
    )
    for registration in registrations:
        registration.release()
    random_registrations = [
        diagnostics.register_secret(value)
        for value in scattered_registry_values(256, 255)
    ]

    def emit_scattered_miss() -> None:
        for _ in range(MATCHER_RECORDS):
            log.emit(MAXIMAL, **miss)

    repeat(
        report,
        "redaction_scattered_registry_miss",
        MATCHER_RECORDS,
        HEAVY_REPEATS,
        emit_scattered_miss,
    )
    diverse = {
        **{f"s{i:02d}": diverse_text(1024, i) for i in range(12)},
        "exception": miss["exception"],
    }

    def emit_scattered_diverse() -> None:
        for _ in range(MATCHER_RECORDS):
            log.emit(MAXIMAL, **diverse)

    repeat(
        report,
        "redaction_scattered_registry_diverse_text",
        MATCHER_RECORDS,
        HEAVY_REPEATS,
        emit_scattered_diverse,
    )
    for registration in random_registrations:
        registration.release()
    near_miss_registrations = [
        diagnostics.register_secret(value) for value in near_miss_values()
    ]
    run = {"s00": "abxxxxx" * 2_300, "exception": miss["exception"]}

    def emit_run() -> None:
        for _ in range(MATCHER_RECORDS):
            log.emit(MAXIMAL, **run)

    repeat(report, "redaction_near_miss_run", MATCHER_RECORDS, HEAVY_REPEATS, emit_run)
    for registration in near_miss_registrations:
        registration.release()
    rounds = iter(range(10**9))

    def rotate() -> None:
        for _ in range(ROTATION_ROUNDS):
            stamp = next(rounds)
            handles = [
                diagnostics.register_secret(f"rotating-{stamp:08d}-{index:02d}-value")
                for index in range(50)
            ]
            for handle in handles:
                handle.release()

    repeat(
        report, "redaction_rotation_50x100", ROTATION_ROUNDS * 50, HEAVY_REPEATS, rotate
    )
    diagnostics.shutdown()
    sink.close()
    report.durations["redaction_workloads"] = time.perf_counter() - started


def masked_feed(
    registry: SecretRegistry, chunks: list[bytes], held: list[int]
) -> Callable[[], None]:
    def feed() -> None:
        with StreamMasker(registry, discard) as masker:
            for chunk in chunks:
                masker.feed(chunk)
                held.append(masker.held)

    return feed


def discard(text: str) -> None:
    return None


def stream_masker_workloads(report: Report) -> None:
    started = time.perf_counter()
    lines = [MASKER_LINE] * MASKER_LINES
    stream = b"".join(lines)
    chunks = [
        stream[start : start + MASKER_CHUNK_BYTES]
        for start in range(0, len(stream), MASKER_CHUNK_BYTES)
    ]
    for count in MASKER_VALUE_COUNTS:
        registry = SecretRegistry()
        registrations = [registry.register(value) for value in registered_values(count)]
        held: list[int] = []
        repeat(
            report,
            f"stream_masker_per_line_{count}_values",
            MASKER_LINES,
            REPEATS,
            masked_feed(registry, lines, held),
        )
        repeat(
            report,
            f"stream_masker_64kib_{count}_values",
            MASKER_LINES,
            REPEATS,
            masked_feed(registry, chunks, held),
        )
        report.environment[f"stream_masker_{count}_values_max_held"] = max(held)
        for registration in registrations:
            registration.release()
    registry = SecretRegistry()
    registration = registry.register(ADVERSARIAL_VALUE)
    held = []
    repeat(
        report,
        "stream_masker_adversarial_prefix_per_line",
        ADVERSARIAL_LINES,
        REPEATS,
        masked_feed(registry, [ADVERSARIAL_LINE] * ADVERSARIAL_LINES, held),
    )
    report.environment["stream_masker_adversarial_max_held"] = max(held)
    small_feeds = [ADVERSARIAL_VALUE[index].encode() for index in range(SMALL_FEEDS)]
    repeat(
        report,
        "stream_masker_small_feeds_with_held_tail",
        SMALL_FEEDS,
        REPEATS,
        masked_feed(registry, small_feeds, held),
    )
    registration.release()
    run_registry = SecretRegistry()
    run_registrations = [
        run_registry.register("a" * length) for length in range(8, 8 + RUN_FORMS)
    ]
    run = "a" * MASKER_CHUNK_BYTES
    repeat(
        report,
        "stream_masker_self_overlapping_run",
        1,
        REPEATS,
        masked_feed(run_registry, [run.encode()], held),
    )
    repeat(
        report,
        "redaction_self_overlapping_record",
        1,
        REPEATS,
        lambda: discard(run_registry.mask(run, 1024)),
    )
    for run_registration in run_registrations:
        run_registration.release()
    near_registry = SecretRegistry()
    near_registrations = [
        near_registry.register("a" * (2000 + index) + "b" + "a" * 2000)
        for index in range(NEAR_MISS_FORMS)
    ]
    near_feeds = [b"a" * 1024] * NEAR_MISS_FEEDS
    repeat(
        report,
        "stream_masker_near_miss_small_feeds",
        NEAR_MISS_FEEDS,
        REPEATS,
        masked_feed(near_registry, near_feeds, held),
    )
    for near_registration in near_registrations:
        near_registration.release()
    report.environment["stream_masker_rss_bytes"] = current_rss_bytes()
    report.durations["stream_masker_workloads"] = time.perf_counter() - started
    report.gates["stream_masker_budget"] = (
        report.durations["stream_masker_workloads"] < MASKER_BUDGET_SECONDS
    )
    report.gates["stream_masker_ordinary_output_not_held"] = all(
        report.environment[f"stream_masker_{count}_values_max_held"] == 0
        for count in MASKER_VALUE_COUNTS
    )


def bridge_and_context_workloads(report: Report) -> None:
    sink = Sink()
    sink.drain()
    diagnostics = configured(LoggingSettings(third_party={"vendor": Level.INFO}), sink)
    log = diagnostics.logger(MEASUREMENT_COMPONENT)
    vendor = logging.getLogger("vendor.lib")
    started = time.perf_counter()

    def bridged() -> None:
        for _ in range(BRIDGED_RECORDS):
            vendor.warning("bridged %s", "argument")

    def context_cycles() -> None:
        for index in range(RECORDS):
            with bound_scope(
                namespace_id="ns",
                graph_id="graph",
                run_id=f"run-{index}",
                node_path="a.b",
                attempt_id="att",
                session_id="sess",
                worker_id="worker",
                request_id="req",
                execution_domain_id="dom",
                replayed_request_id="orig",
                lease_epoch=index,
            ):
                log.emit(WIDE, f00="context")

    repeat(report, "bridged_stdlib_records", BRIDGED_RECORDS, REPEATS, bridged)
    repeat(report, "context_bind_emit_reset", RECORDS, REPEATS, context_cycles)
    diagnostics.shutdown()
    sink.close()
    report.durations["bridge_and_context_workloads"] = time.perf_counter() - started


def saturation_workloads(report: Report) -> None:
    sink = Sink()
    diagnostics = configured(LoggingSettings(), sink)
    log = diagnostics.logger(MEASUREMENT_COMPONENT)
    fields = wide_fields()
    started = time.perf_counter()
    per_thread = RECORDS // PRODUCER_THREADS
    bound_respected = True

    def produce() -> None:
        for _ in range(per_thread):
            log.emit(WIDE, **fields)

    def contended() -> None:
        nonlocal bound_respected
        threads = [threading.Thread(target=produce) for _ in range(PRODUCER_THREADS)]
        for thread in threads:
            thread.start()
        while any(thread.is_alive() for thread in threads):
            snapshot = diagnostics.snapshot()
            if snapshot.queue_records > diagnostics.settings.queue_records:
                bound_respected = False
            if snapshot.queue_bytes > diagnostics.settings.queue_bytes:
                bound_respected = False
            time.sleep(0.001)
        for thread in threads:
            thread.join()

    repeat(report, "four_producers_saturated_queue", RECORDS, HEAVY_REPEATS, contended)
    report.gates["queue_bounds_respected"] = bound_respected
    sink.drain()
    deadline = time.monotonic() + 30
    while diagnostics.snapshot().queue_records and time.monotonic() < deadline:
        time.sleep(0.01)

    def throughput() -> None:
        consumed_before = sink.consumed
        dropped_before = diagnostics.snapshot().dropped_total()
        for _ in range(RECORDS):
            log.emit(WIDE, **fields)
        while diagnostics.snapshot().queue_records and time.monotonic() < deadline + 60:
            time.sleep(0.001)
        settled = sink.consumed
        while time.monotonic() < deadline + 60:
            time.sleep(0.02)
            if sink.consumed == settled:
                break
            settled = sink.consumed
        report.durations["writer_bytes_last_run"] = float(settled - consumed_before)
        report.durations["writer_drops_last_run"] = float(
            diagnostics.snapshot().dropped_total() - dropped_before
        )

    repeat(report, "writer_throughput_fast_reader", RECORDS, HEAVY_REPEATS, throughput)
    diagnostics.shutdown()
    sink.close()
    report.durations["saturation_workloads"] = time.perf_counter() - started


def exit_on_full_pipe(report: Report) -> bool:
    process = subprocess.Popen(
        [sys.executable, "-c", FULL_PIPE_SCRIPT],
        env={"PATH": os.environ["PATH"]},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    started = time.perf_counter()
    code = process.wait(timeout=60)
    elapsed = time.perf_counter() - started
    assert process.stdout is not None
    assert process.stderr is not None
    shutdown_seconds = float(process.stdout.read().decode().strip() or "nan")
    process.stdout.close()
    failure = "" if code == 0 else process.stderr.read()[-400:].decode(errors="replace")
    process.stderr.close()
    report.samples.append(
        Sample(
            "exit_on_full_pipe",
            0,
            1,
            elapsed,
            {"code": code, "shutdown_seconds": shutdown_seconds, "failure": failure},
        )
    )
    bound = LoggingSettings().drain_seconds + EXIT_MARGIN_SECONDS
    return code == 0 and shutdown_seconds < bound and elapsed < bound


def idle_shutdown(report: Report) -> bool:
    sink = Sink()
    sink.drain()
    diagnostics = configured(LoggingSettings(), sink)
    deadline = time.monotonic() + 30
    while diagnostics.snapshot().queue_records and time.monotonic() < deadline:
        time.sleep(0.001)

    def shut() -> None:
        diagnostics.shutdown()

    elapsed = timed(shut)
    sink.close()
    report.samples.append(Sample("idle_shutdown", 0, 1, elapsed))
    return elapsed < IDLE_SHUTDOWN_SECONDS


def configure_shutdown_cycles(report: Report) -> dict[str, bool]:
    gates: dict[str, bool] = {}
    sink = Sink()
    sink.drain()

    def cycle() -> None:
        diagnostics = configured(LoggingSettings(), sink)
        diagnostics.logger(MEASUREMENT_COMPONENT).emit(WIDE, f00="cycle")
        diagnostics.shutdown()

    def run_cycles() -> None:
        for _ in range(CYCLES):
            cycle()

    cycle()
    threads_before, descriptors_before = settled_counts()
    gc.collect()
    tracemalloc.start()
    heap_before = tracemalloc.get_traced_memory()[0]
    elapsed = timed(run_cycles)
    gc.collect()
    heap_middle = tracemalloc.get_traced_memory()[0]
    run_cycles()
    gc.collect()
    heap_after = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    first_half = heap_middle - heap_before
    second_half = heap_after - heap_middle
    report.samples.append(
        Sample(
            "configure_shutdown_cycles_traced",
            0,
            CYCLES,
            elapsed,
            {
                "heap_growth_first_half_bytes": first_half,
                "heap_growth_second_half_bytes": second_half,
            },
        )
    )
    rss_before = current_rss_bytes()
    elapsed_plain = timed(run_cycles)
    rss_after = current_rss_bytes()
    rss_growth = (
        None if rss_before is None or rss_after is None else rss_after - rss_before
    )
    report.samples.append(
        Sample(
            "configure_shutdown_cycles_plain",
            0,
            CYCLES,
            elapsed_plain,
            {"rss_growth_bytes": rss_growth, "rss_available": rss_growth is not None},
        )
    )
    threads_after, descriptors_after = settled_counts()
    sink.close()
    report.samples.append(
        Sample(
            "configure_shutdown_cycles_counts",
            0,
            CYCLES,
            0.0,
            {
                "threads_before": threads_before,
                "threads_after": threads_after,
                "descriptors_before": descriptors_before,
                "descriptors_after": descriptors_after,
            },
        )
    )
    gates["threads_return_to_baseline"] = threads_after == threads_before
    gates["descriptors_return_to_baseline"] = descriptors_after == descriptors_before
    gates["heap_growth_bounded"] = (
        heap_after - heap_before < HEAP_GROWTH_ALLOWANCE_BYTES
        and second_half <= HEAP_SLOPE_ALLOWANCE_BYTES
    )
    gates["rss_growth_bounded"] = (
        rss_growth is None or rss_growth < RSS_GROWTH_ALLOWANCE_BYTES
    )
    return gates


def cold_start(report: Report) -> None:
    for index in range(REPEATS):
        started = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, "-m", "kora", "--version"],
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            timeout=60,
            check=False,
        )
        elapsed = time.perf_counter() - started
        report.samples.append(
            Sample("cli_cold_start", index, 1, elapsed, {"code": completed.returncode})
        )
