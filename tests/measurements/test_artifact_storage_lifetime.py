import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from tests.measurements.cel_measurement_process import run_process
from tests.measurements.storage_checkpoint import partial_report
from tests.measurements.storage_lifetime_manifest import storage_report_json
from tests.plugins.storage.pressure_native import command

pytestmark = pytest.mark.measurement


@pytest.mark.parametrize("repeat", range(3))
def test_artifact_storage_lifetime(tmp_path: Path, repeat: int) -> None:
    destination = Path(os.environ.get("KORA_STORAGE_EVIDENCE", str(tmp_path)))
    output = destination / f"{sys.platform}-{repeat}"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="owned-", dir=tmp_path) as temporary:
        arguments = [
            sys.executable,
            "-m",
            "tests.measurements.storage_lifetime_child",
            str(output),
            str(repeat),
            str(Path(temporary) / "store"),
            os.environ.get(
                "KORA_MEASUREMENT_REVISION",
                command(("git", "rev-parse", "HEAD")).decode().strip(),
            ),
            os.environ.get("KORA_STORAGE_LOAD_CONTEXT", "local_unspecified"),
        ]

        result = run_process(arguments, deadline_seconds=480, output_limit=1048576)
        (output / "stdout.txt").write_bytes(result.stdout)
        (output / "stderr.txt").write_bytes(result.stderr)
        (output / "execution.json").write_text(
            json.dumps(
                {
                    "pid": result.pid,
                    "status": result.code,
                    "timed_out": result.timed_out,
                    "overflow": result.overflow,
                }
            ),
            encoding="utf-8",
        )
        if (output / "checkpoint.json").exists():
            (output / "partial-report.json").write_text(
                storage_report_json(partial_report(output)), encoding="utf-8"
            )

    assert result.code == 0, result.stderr.decode()
    assert not result.timed_out
    assert not result.overflow
    assert result.stdout == b""
    assert (output / "report.json").stat().st_size > 0
