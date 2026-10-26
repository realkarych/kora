import os
from array import array
from pathlib import Path

import pytest

from tests.measurements.harness import new_report
from tests.measurements.storage_checkpoint import (
    MAGIC,
    ROW,
    WIDTH,
    append_checkpoint,
    partial_report,
    read_checkpoint,
    start_checkpoint,
)
from tests.measurements.storage_evidence_common import EvidenceFailure
from tests.measurements.storage_lifetime_manifest import samples


def write_rows(directory: Path, count: int) -> None:
    report = new_report()
    report.platform = "linux"
    report.revision = "a" * 40
    report.samples = samples(0)
    report.environment.update(source_digest="b" * 64, pid=os.getpid())
    numbers = array("d", [0.0]) * (len(report.samples) * WIDTH)
    executed = bytearray([1]) * len(report.samples)
    captured = bytearray([1]) * len(report.samples)
    descriptor = os.open(directory / "rows.bin", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        start_checkpoint(directory, descriptor, report, 0)
        for index in range(count):
            numbers[index * WIDTH] = index + 0.5
            append_checkpoint(descriptor, index, numbers, executed, captured)
    finally:
        os.close(descriptor)


def test_checkpoint_recovers_only_complete_rows_without_passing_gates(
    tmp_path: Path,
) -> None:
    write_rows(tmp_path, 3)
    with (tmp_path / "rows.bin").open("ab") as stream:
        stream.write(ROW.pack(3, 1, 1) + b"\0" * 7)

    header, rows, tail = read_checkpoint(tmp_path)
    recovered = partial_report(tmp_path)

    assert header.row_count == len(samples(0))
    assert [row.values[0] for row in rows] == [0.5, 1.5, 2.5]
    assert tail == ROW.size + 7
    assert not any(recovered.gates.values())
    assert all(sample.extra["executed"] for sample in recovered.samples[:3])
    assert not any(sample.extra["executed"] for sample in recovered.samples[3:])


@pytest.mark.parametrize(
    "defect", ["identity", "duplicate", "unexecuted", "nonfinite", "fractional"]
)
def test_checkpoint_refuses_inconsistent_numeric_records(
    tmp_path: Path, defect: str
) -> None:
    write_rows(tmp_path, 2)
    path = tmp_path / "rows.bin"
    encoded = bytearray(path.read_bytes())
    offset = len(MAGIC) + 65
    if defect == "identity":
        encoded[len(MAGIC)] = ord("g")
    elif defect == "duplicate":
        encoded[
            offset + ROW.size + WIDTH * 8 : offset + ROW.size + WIDTH * 8 + ROW.size
        ] = ROW.pack(0, 1, 1)
    elif defect == "unexecuted":
        encoded[offset : offset + ROW.size] = ROW.pack(0, 0, 1)
    else:
        values = array("d", [0.0]) * WIDTH
        values[0 if defect == "nonfinite" else 1] = (
            float("nan") if defect == "nonfinite" else 0.5
        )
        encoded[offset + ROW.size : offset + ROW.size + WIDTH * 8] = values.tobytes()
    path.write_bytes(encoded)

    with pytest.raises(EvidenceFailure):
        partial_report(tmp_path)
