import os
import time
from collections.abc import Iterator

import pytest

from kora.conformance.capture import MAX_CAPTURE_BYTES
from kora.observability import (
    ComponentLogger,
    LoggingSettings,
    LossReason,
    ProcessDiagnostics,
)
from kora.observability.sink import BATCH_BYTES
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    configured_with_reader,
    run_python,
    wait_until,
)

BURST_RECORDS: int = 2000
FILL_CHUNKS: tuple[int, ...] = (65_536, 4096, 512, 1)
CAPTURE_BYTES: int = 4 * MAX_CAPTURE_BYTES
SMALL_QUEUE: LoggingSettings = LoggingSettings(
    queue_records=32, queue_bytes=65_536, max_record_bytes=4096
)

LOSSY_CLI_SCRIPT: str = """
import datetime
from kora.observability import CATALOG, EventSpec, FieldKind, Level, LoggingSettings
from kora.observability import configure_process
component = CATALOG.register_component("lossy")
spec = CATALOG.register(
    EventSpec("lossy.note", component, Level.INFO, "note", {"text": FieldKind.STRING})
)
diagnostics = configure_process(
    component, LoggingSettings(), "lossy", lambda: datetime.datetime.now(datetime.UTC)
)
log = diagnostics.logger(component)
log.emit(spec, text="ok")
log.emit(spec, text="x" * 20000)
log.emit(spec, text="ok")
diagnostics.shutdown()
"""


@pytest.fixture
def pipe() -> Iterator[Pipe]:
    created = Pipe.open(max_bytes=CAPTURE_BYTES)
    yield created
    created.release()


def filled(descriptor: int) -> int:
    os.set_blocking(descriptor, False)
    written = 0
    for chunk in FILL_CHUNKS:
        while True:
            try:
                written += os.write(descriptor, b"\n" * chunk)
            except BlockingIOError:
                break
    os.set_blocking(descriptor, True)
    return written


def delivered_note(pipe: Pipe) -> bool:
    return any(record.get("event") == NOTE.name for record in pipe.reader.records())


def emitted_until_blocked(
    diagnostics: ProcessDiagnostics, log: ComponentLogger
) -> bool:
    log.emit(NOTE, text="y" * 300)
    state = diagnostics.snapshot()
    return (
        state.queue_records == SMALL_QUEUE.queue_records and state.in_flight_bytes > 0
    )


def block_the_writer(diagnostics: ProcessDiagnostics, log: ComponentLogger) -> None:
    for _ in range(BURST_RECORDS):
        log.emit(NOTE, text="y" * 300)
    wait_until(lambda: emitted_until_blocked(diagnostics, log))


def descriptor_identity(descriptor: int) -> tuple[int, int]:
    status = os.fstat(descriptor)
    return (status.st_dev, status.st_ino)


def test_state_reports_loss_while_the_sink_is_blocked(
    configure: Configure, pipe: Pipe
) -> None:
    prefilled = filled(pipe.write_end)
    diagnostics = configure(SMALL_QUEUE, pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    block_the_writer(diagnostics, log)
    log.emit(NOTE, text="y" * 300)
    log.emit(NOTE, text="x" * 5000)

    blocked = diagnostics.snapshot()
    pipe.reader.start()
    wait_until(diagnostics.writer_is_idle)
    after = diagnostics.snapshot()
    wait_until(lambda: delivered_note(pipe))
    discarded = pipe.reader.discarded

    assert 0 < prefilled < CAPTURE_BYTES
    assert discarded == 0
    assert blocked.queue_records == SMALL_QUEUE.queue_records
    assert 0 < blocked.queue_bytes <= SMALL_QUEUE.queue_bytes
    assert blocked.in_flight_bytes > 0
    assert blocked.dropped_total() > 0
    assert blocked.rejected[(TEST_COMPONENT, "INFO", LossReason.OVER_BUDGET.value)] == 1
    assert blocked.suppressed_total() > 0
    assert not blocked.sink_failed
    assert after.queue_records == 0
    assert after.in_flight_bytes == 0
    assert after.dropped_total() == blocked.dropped_total()
    assert not after.sink_failed


def test_the_in_flight_batch_is_reported_and_cleared(
    configure: Configure, pipe: Pipe
) -> None:
    prefilled = filled(pipe.write_end)
    diagnostics = configure(SMALL_QUEUE, pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    block_the_writer(diagnostics, log)

    held = diagnostics.snapshot().in_flight_bytes
    pipe.close_read_end()
    wait_until(lambda: not diagnostics.writer_is_alive())
    failed = diagnostics.snapshot()

    assert prefilled > 0
    assert 0 < held <= BATCH_BYTES
    assert failed.sink_failed
    assert failed.in_flight_bytes == 0
    assert failed.queue_records == 0


def test_cli_shaped_process_emits_one_loss_summary_at_shutdown() -> None:
    result = run_python(["-c", LOSSY_CLI_SCRIPT], {})

    records = result.records()
    events = [record["event"] for record in records]
    summary = next(
        record for record in records if record["event"] == "logging.loss_summary"
    )
    assert result.code == 0
    assert events.count("logging.loss_summary") == 1
    assert events[-1] == "logging.loss_summary"
    assert summary["rejected"] == 1
    assert summary["dropped"] == 0
    assert events.count("lossy.note") == 2


def test_idle_shutdown_returns_before_the_drain_deadline(
    configure: Configure, pipe: Pipe
) -> None:
    settings = LoggingSettings()
    sink_identity = descriptor_identity(pipe.write_end)
    diagnostics = configured_with_reader(configure, pipe, settings)
    wait_until(lambda: diagnostics.snapshot().queue_records == 0)

    started = time.monotonic()
    drained = diagnostics.shutdown()
    elapsed = time.monotonic() - started

    assert drained
    assert elapsed < settings.drain_seconds
    assert not diagnostics.writer_is_alive()
    assert descriptor_identity(pipe.write_end) == sink_identity
