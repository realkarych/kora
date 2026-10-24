import json
import os
import sys
from pathlib import Path

import pytest

from tests.measurements.cel_measurement_process import run_process
from tests.measurements.harness import Report, Sample
from tests.plugins.storage.sink_probe import SinkReport


@pytest.mark.parametrize("sink", ["blocked", "broken"])
@pytest.mark.parametrize("level", ["INFO", "DEBUG", "CRITICAL"])
def test_storage_cleanup_and_exit_progress_with_unavailable_sink(
    tmp_path: Path, sink: str, level: str, boundary_report: Report
) -> None:
    arguments = [
        sys.executable,
        "-m",
        "tests.plugins.storage.sink_probe",
        str(tmp_path / "store"),
        sink,
        level,
    ]

    completed = run_process(arguments, deadline_seconds=15)
    report_path = os.environ.get("KORA_STORAGE_BOUNDARY_OUTPUT")
    evidence = (
        Path(report_path).parent / "sinks" if report_path else tmp_path / "evidence"
    )
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / f"{sink}-{level}.stdout").write_bytes(completed.stdout)
    (evidence / f"{sink}-{level}.stderr").write_bytes(completed.stderr)
    (evidence / f"{sink}-{level}.execution.json").write_text(
        json.dumps(
            {
                "pid": completed.pid,
                "status": completed.code,
                "timed_out": completed.timed_out,
                "overflow": completed.overflow,
            }
        ),
        encoding="utf-8",
    )

    assert completed.code == 0, completed.stderr
    assert not completed.timed_out
    assert not completed.overflow
    assert completed.stderr == b""
    report = SinkReport.model_validate_json(completed.stdout)
    assert report.completed_refusals == 150
    assert report.payload_verified
    assert report.provider_closed
    assert report.queue_records == 0
    assert report.queue_bytes == 0
    if level != "CRITICAL" and sink == "blocked":
        assert report.dropped > 0
        assert not report.drained
        assert report.writer_alive_at_exit
    if level != "CRITICAL" and sink == "broken":
        assert report.sink_failed
        assert report.drained
    name = f"storage.sink.{sink}.{level}"
    boundary_report.samples.append(
        Sample(
            name,
            0,
            report.completed_refusals,
            report.elapsed_seconds,
            {
                "dropped": report.dropped,
                "sink_failed": report.sink_failed,
                "queue_records": report.queue_records,
                "queue_bytes": report.queue_bytes,
                "drained": report.drained,
                "writer_alive_at_exit": report.writer_alive_at_exit,
                "filled_pipe_bytes": report.filled_pipe_bytes,
                "provider_closed": report.provider_closed,
                "payload_verified": report.payload_verified,
                "actual_exit_status": completed.code,
            },
        )
    )
    boundary_report.gates[name] = True
