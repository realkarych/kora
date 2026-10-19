import os
import time

from kora.observability import LoggingSettings, LossReason
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    run_python,
    wait_until,
)

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


def test_state_reports_loss_while_the_sink_is_blocked(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(SMALL_QUEUE, pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)

    for _ in range(2000):
        log.emit(NOTE, text="y" * 300)
    log.emit(NOTE, text="x" * 5000)
    blocked = diagnostics.snapshot()
    pipe.reader.start()
    wait_until(lambda: diagnostics.snapshot().queue_records == 0)
    after = diagnostics.snapshot()

    assert blocked.queue_records == SMALL_QUEUE.queue_records
    assert blocked.queue_bytes > 0
    assert blocked.dropped_total() > 0
    assert blocked.rejected[(TEST_COMPONENT, "INFO", LossReason.OVER_BUDGET.value)] == 1
    assert blocked.suppressed_total() > 0
    assert not blocked.sink_failed
    assert after.queue_records == 0
    assert after.dropped_total() == blocked.dropped_total()


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


def test_idle_shutdown_returns_at_once(configure: Configure, pipe: Pipe) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    pipe.reader.start()
    wait_until(lambda: diagnostics.snapshot().queue_records == 0)

    started = time.monotonic()
    drained = diagnostics.shutdown()
    elapsed = time.monotonic() - started

    assert drained
    assert elapsed < 0.5
    assert not diagnostics.writer_is_alive()
    assert os.fstat(pipe.write_end).st_size >= 0
