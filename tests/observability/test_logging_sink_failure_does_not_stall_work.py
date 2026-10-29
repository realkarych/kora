import json
import os
import select
import subprocess
import sys
import time

import pytest

from kora.observability import AlreadyConfiguredError, LoggingSettings, LossReason
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    finished_output,
    wait_until,
)

SMALL_QUEUE: LoggingSettings = LoggingSettings(
    queue_records=64, queue_bytes=65_536, max_record_bytes=4096
)
SMALL_BYTES_QUEUE: LoggingSettings = LoggingSettings(
    queue_records=10_000, queue_bytes=8500, max_record_bytes=4096
)
WIDE_RECORD_TEXT: str = "w" * 3800
FILL_RECORDS: int = 5000
RECORD_TEXT: str = "x" * 200
DRAIN_MARGIN_SECONDS: float = 2.0
BLOCKING_RECORDS: int = 100

FULL_PIPE_SCRIPT: str = """
import datetime, sys
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
import time
for index in range(20000):
    log.emit(spec, text="y" * 500)
started = time.monotonic()
drained = diagnostics.shutdown()
sys.stdout.write("drained=%s shutdown=%.3f\\n" % (drained, time.monotonic() - started))
"""


def test_producers_keep_going_while_the_reader_is_stopped(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(SMALL_QUEUE, pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)

    for _ in range(FILL_RECORDS):
        log.emit(NOTE, text=RECORD_TEXT)
    snapshot = diagnostics.snapshot()

    assert snapshot.queue_records <= SMALL_QUEUE.queue_records
    assert snapshot.queue_bytes <= SMALL_QUEUE.queue_bytes
    dropped = snapshot.dropped
    assert dropped[(TEST_COMPONENT, "INFO", LossReason.QUEUE_RECORDS.value)] > 0
    assert snapshot.dropped_total() + snapshot.queue_records <= FILL_RECORDS + 2
    assert not snapshot.sink_failed


@pytest.mark.parametrize(
    ("settings", "text", "reason", "drop_reports"),
    [
        (SMALL_QUEUE, RECORD_TEXT, LossReason.QUEUE_RECORDS, {0, 1}),
        (SMALL_BYTES_QUEUE, WIDE_RECORD_TEXT, LossReason.QUEUE_BYTES, {1}),
    ],
)
def test_recovery_after_a_stopped_reader_reports_drops_once(
    configure: Configure,
    pipe: Pipe,
    settings: LoggingSettings,
    text: str,
    reason: LossReason,
    drop_reports: set[int],
) -> None:
    diagnostics = configure(settings, pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    for _ in range(FILL_RECORDS):
        log.emit(NOTE, text=text)
    dropped_before = diagnostics.snapshot().dropped_total()

    pipe.reader.start()
    wait_until(lambda: diagnostics.snapshot().queue_records == 0)
    log.emit(NOTE, text="after-recovery")
    finished_output(diagnostics, pipe)

    records = pipe.reader.records()
    events = [record["event"] for record in records]
    snapshot = diagnostics.snapshot()
    assert events.count("logging.records_dropped") in drop_reports
    assert events.count("logging.sink_recovered") == 1
    dropped = [r for r in records if r["event"] == "logging.records_dropped"]
    assert all(record["reason"] == reason.value for record in dropped)
    recovery = next(r for r in records if r["event"] == "logging.sink_recovered")
    assert 0 < int(str(recovery["dropped"])) <= dropped_before
    assert snapshot.sink_recoveries >= 1
    assert snapshot.dropped.get((TEST_COMPONENT, "INFO", reason.value), 0) > 0
    assert snapshot.dropped_total() > 0


def test_a_writer_blocked_past_its_deadline_keeps_the_stream(
    configure: Configure, pipe: Pipe
) -> None:
    settings = LoggingSettings(drain_seconds=0.1, max_record_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    for _ in range(BLOCKING_RECORDS):
        log.emit(NOTE, text="y" * 4000)
    wait_until(lambda: not diagnostics.writer_is_idle())

    drained = diagnostics.shutdown()
    with pytest.raises(AlreadyConfiguredError):
        configure(settings, pipe.write_end)
    pipe.reader.start()
    wait_until(lambda: not diagnostics.writer_is_alive())
    reconfigured = configure(settings, pipe.write_end)

    assert not drained
    assert reconfigured is not diagnostics
    assert reconfigured.writer_is_alive()


def test_records_larger_than_pipe_buf_parse_under_a_slow_reader(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    pipe.reader.start(chunk=97)
    big = {f"k{index}": "v" * 700 for index in range(10)}

    for _ in range(50):
        log.emit(NOTE, detail=big)
    pipe.reader.wait_for_lines(50)

    lines = pipe.reader.lines()
    assert all(len(line) > select.PIPE_BUF for line in lines)
    assert all(json.loads(line)["detail"] == big for line in lines)


def test_closed_reader_marks_the_sink_failed_and_releases_the_queue(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(SMALL_QUEUE, pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    pipe.close_read_end()

    for _ in range(200):
        log.emit(NOTE, text=RECORD_TEXT)
    wait_until(lambda: diagnostics.snapshot().sink_failed)
    log.emit(NOTE, text="after-failure")
    snapshot = diagnostics.snapshot()
    started = time.monotonic()
    drained = diagnostics.shutdown()
    elapsed = time.monotonic() - started

    assert snapshot.sink_failures == 1
    assert snapshot.queue_records == 0
    assert snapshot.dropped[(TEST_COMPONENT, "INFO", LossReason.SINK_FAILED.value)] >= 1
    assert drained
    assert elapsed < DRAIN_MARGIN_SECONDS
    assert not diagnostics.writer_is_alive()


def test_nonblocking_inherited_descriptor_is_written_completely(
    configure: Configure, pipe: Pipe
) -> None:
    os.set_blocking(pipe.write_end, False)
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)

    for index in range(300):
        log.emit(NOTE, text=f"{index:04d}" + "z" * 900)
    pipe.reader.start()
    pipe.reader.wait_for_lines(300)

    texts = [json.loads(line)["text"][:4] for line in pipe.reader.lines()]
    assert texts == [f"{index:04d}" for index in range(300)]
    assert not diagnostics.snapshot().sink_failed


def test_real_process_exits_within_the_drain_deadline_on_a_full_pipe() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", FULL_PIPE_SCRIPT],
        env={"PATH": os.environ["PATH"]},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    started = time.monotonic()

    code = process.wait(timeout=30)
    elapsed = time.monotonic() - started
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = process.stdout.read()
    process.stderr.close()
    process.stdout.close()

    assert code == 0
    drained, shutdown = stdout.decode().split()
    assert drained == "drained=False"
    assert (
        float(shutdown.removeprefix("shutdown="))
        < LoggingSettings().drain_seconds + 0.5
    )
    assert elapsed < 30
