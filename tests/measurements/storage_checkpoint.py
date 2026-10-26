import hashlib
import math
import os
import struct
import sys
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, StrictInt, StrictStr

from kora.protocols.common import BoundaryModel
from tests.measurements.harness import ExtraValue, Report
from tests.measurements.storage_evidence_common import EvidenceFailure, read_json
from tests.measurements.storage_lifetime_manifest import (
    RESOURCE_FIELDS,
    TIMING_FIELDS,
    WORKLOADS,
    samples,
)

ROW: Final[struct.Struct] = struct.Struct("<QBB")
MAGIC: Final[bytes] = b"KORA-STORAGE-ROWS-v1\n"
WIDTH: Final[int] = 1 + len(RESOURCE_FIELDS) + len(TIMING_FIELDS)


class CheckpointHeader(BoundaryModel):
    version: Annotated[StrictInt, Field(ge=1, le=1)]
    run_id: Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]
    repeat: Annotated[StrictInt, Field(ge=0, le=2)]
    width: StrictInt
    row_count: StrictInt
    byteorder: Literal["little", "big"]
    started_at: StrictStr
    platform: Literal["linux", "darwin"]
    python: StrictStr
    revision: StrictStr
    environment: dict[str, ExtraValue]


@dataclass(frozen=True)
class CheckpointRow:
    index: int
    executed: bool
    captured: bool
    values: tuple[float, ...]


def start_checkpoint(
    output: Path, descriptor: int, report: Report, repeat: int
) -> None:
    identity = hashlib.sha256(
        f"{report.revision}:{report.started_at}:{os.getpid()}:{repeat}".encode()
    ).hexdigest()
    header = CheckpointHeader.model_validate(
        {
            "version": 1,
            "run_id": identity,
            "repeat": repeat,
            "width": WIDTH,
            "row_count": len(report.samples),
            "byteorder": sys.byteorder,
            "started_at": report.started_at,
            "platform": report.platform,
            "python": report.python,
            "revision": report.revision,
            "environment": dict(report.environment),
        }
    )
    (output / "checkpoint.json").write_text(header.model_dump_json(), encoding="utf-8")
    encoded = MAGIC + identity.encode() + b"\n"
    if os.write(descriptor, encoded) != len(encoded):
        raise OSError("the checkpoint header write was incomplete")


def append_checkpoint(
    descriptor: int,
    index: int,
    numbers: "array[float]",
    executed: bytearray,
    captured: bytearray,
) -> None:
    header = ROW.pack(index, executed[index], captured[index])
    with memoryview(numbers) as source:
        with source[index * WIDTH : (index + 1) * WIDTH].cast("B") as values:
            if os.writev(descriptor, (header, values)) != len(header) + len(values):
                raise OSError("the checkpoint row write was incomplete")


def read_checkpoint(
    output: Path,
) -> tuple[CheckpointHeader, tuple[CheckpointRow, ...], int]:
    header = CheckpointHeader.model_validate(
        read_json(output / "checkpoint.json", 16384)
    )
    if header.width != WIDTH or header.row_count != len(samples(header.repeat)):
        raise EvidenceFailure("the storage checkpoint layout differs from its manifest")
    prefix = MAGIC + header.run_id.encode() + b"\n"
    maximum = len(prefix) + header.row_count * (ROW.size + WIDTH * 8)
    with (output / "rows.bin").open("rb") as stream:
        encoded = stream.read(maximum + 1)
    if len(encoded) > maximum or not encoded.startswith(prefix):
        raise EvidenceFailure("the checkpoint run identity or size is invalid")
    body = memoryview(encoded)[len(prefix) :]
    size = ROW.size + WIDTH * 8
    complete = len(body) // size
    rows: list[CheckpointRow] = []
    for index in range(complete):
        position, executed, captured = ROW.unpack_from(body, index * size)
        if position != index or executed != 1 or captured not in {0, 1}:
            raise EvidenceFailure("checkpoint rows are missing, duplicated or invalid")
        values = array("d")
        values.frombytes(body[index * size + ROW.size : (index + 1) * size])
        if header.byteorder != sys.byteorder:
            values.byteswap()
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise EvidenceFailure("checkpoint numeric values are invalid")
        rows.append(CheckpointRow(index, True, bool(captured), tuple(values)))
    return header, tuple(rows), len(body) % size


def partial_report(output: Path) -> Report:
    header, rows, _ = read_checkpoint(output)
    report = Report(
        header.started_at,
        header.platform,
        header.python,
        header.revision,
        environment=dict(header.environment),
    )
    report.samples = samples(header.repeat)
    report.gates = dict.fromkeys(
        (*[workload.name for workload in WORKLOADS], "complete"), False
    )
    for row in rows:
        sample = report.samples[row.index]
        sample.seconds = row.values[0]
        sample.extra["executed"] = row.executed
        sample.extra["resource_sample"] = row.captured
        for index, name in enumerate(RESOURCE_FIELDS):
            value = row.values[1 + index]
            if not value.is_integer():
                raise EvidenceFailure("a checkpoint resource counter is not integral")
            sample.extra[name] = int(value)
        for index, name in enumerate(TIMING_FIELDS):
            sample.extra[name] = row.values[1 + len(RESOURCE_FIELDS) + index]
    return report
