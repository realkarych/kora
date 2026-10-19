import json
import os
import select
import subprocess
import sys
import time

from kora.observability import LoggingSettings, LossReason
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    wait_until,
)

SMALL_QUEUE: LoggingSettings = LoggingSettings(
    queue_records=64, queue_bytes=65_536, max_record_bytes=4096
)
FILL_RECORDS: int = 5000
RECORD_TEXT: str = "x" * 200
DRAIN_MARGIN_SECONDS: float = 2.0

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

    started = time.monotonic()
    for _ in range(FILL_RECORDS):
        log.emit(NOTE, text=RECORD_TEXT)
    elapsed = time.monotonic() - started
    snapshot = diagnostics.snapshot()

    assert elapsed < 5.0
    assert snapshot.queue_records <= SMALL_QUEUE.queue_records
    assert snapshot.queue_bytes <= SMALL_QUEUE.queue_bytes
    dropped = snapshot.dropped
    assert dropped[(TEST_COMPONENT, "INFO", LossReason.QUEUE_RECORDS.value)] > 0
    assert snapshot.dropped_total() + snapshot.queue_records <= FILL_RECORDS + 2
    assert not snapshot.sink_failed


def test_recovery_after_a_stopped_reader_reports_drops_once(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(SMALL_QUEUE, pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    for _ in range(FILL_RECORDS):
        log.emit(NOTE, text=RECORD_TEXT)
    dropped_before = diagnostics.snapshot().dropped_total()

    pipe.reader.start()
    wait_until(lambda: diagnostics.snapshot().queue_records == 0)
    log.emit(NOTE, text="after-recovery")
    diagnostics.shutdown()
    pipe.close_write_end()
    pipe.reader.wait_closed()

    records = pipe.reader.records()
    events = [record["event"] for record in records]
    assert events.count("logging.records_dropped") <= 1
    assert events.count("logging.sink_recovered") == 1
    recovery = next(r for r in records if r["event"] == "logging.sink_recovered")
    assert 0 < int(str(recovery["dropped"])) <= dropped_before
    assert all(json.dumps(record) for record in records)
    assert diagnostics.snapshot().sink_recoveries >= 1


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
