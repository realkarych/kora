import hashlib
import json
import os
import sys
from array import array
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from tests.measurements.storage_checkpoint import MAGIC, ROW, WIDTH, CheckpointHeader
from tests.measurements.storage_lifetime_manifest import (
    RESOURCE_FIELDS,
    TIMING_FIELDS,
    WORKLOADS,
)
from tests.measurements.storage_measurement_evidence import StorageReport
from tests.measurements.storage_qualification_evidence import (
    QualificationReport,
    SinkSample,
    qualification_nodes,
)
from tests.plugins.storage.pressure_models import (
    ImageStamp,
    PressureManifest,
    PressureReport,
    ProcessToken,
)
from tests.plugins.storage.sink_probe import SinkReport


def write_junit(path: Path, identities: frozenset[str]) -> None:
    suite = Element("testsuite")
    for identity in sorted(identities):
        module, test = identity.rsplit(".test_", 1)
        SubElement(suite, "testcase", {"classname": module, "name": "test_" + test})
    path.write_bytes(tostring(suite))


def write_execution(path: Path, pid: int = 1234) -> None:
    path.write_text(
        json.dumps({"pid": pid, "status": 0, "timed_out": False, "overflow": False})
    )


def write_lifetime(directory: Path, report: StorageReport) -> None:
    directory.mkdir(parents=True)
    (directory / "report.json").write_text(report.model_dump_json())
    identity = hashlib.sha256(str(directory).encode()).hexdigest()
    header = CheckpointHeader.model_validate(
        {
            "version": 1,
            "run_id": identity,
            "repeat": report.environment.producer,
            "width": WIDTH,
            "row_count": len(report.samples),
            "byteorder": sys.byteorder,
            "started_at": report.started_at,
            "platform": report.platform,
            "python": report.python,
            "revision": report.revision,
            "environment": report.environment.model_dump(mode="json"),
        }
    )
    (directory / "checkpoint.json").write_text(header.model_dump_json())
    with (directory / "rows.bin").open("wb") as stream:
        stream.write(MAGIC + identity.encode() + b"\n")
        for index, sample in enumerate(report.samples):
            values = array(
                "d",
                (
                    sample.seconds,
                    *(
                        getattr(sample, name)
                        for name in (*RESOURCE_FIELDS, *TIMING_FIELDS)
                    ),
                ),
            )
            stream.write(
                ROW.pack(index, int(sample.executed), int(sample.resource_sample))
            )
            stream.write(values.tobytes())
    write_junit(
        directory / "measurement.xml",
        frozenset(
            {
                "tests.measurements.test_artifact_storage_lifetime.test_artifact_storage_lifetime["
                + str(report.environment.producer)
                + "]"
            }
        ),
    )
    write_execution(directory / "execution.json", report.environment.pid)
    for name in ("stdout.txt", "stderr.txt"):
        (directory / name).write_bytes(b"")
    for workload in WORKLOADS:
        if workload.cycles == 100:
            for cycle in (50, 100):
                for suffix in ("snapshot", "txt"):
                    (directory / f"{workload.name}-{cycle}.{suffix}").write_bytes(
                        b"parser fixture"
                    )


def write_qualification(directory: Path, report: QualificationReport) -> None:
    directory.mkdir(parents=True)
    (directory / "boundaries.json").write_text(report.model_dump_json())
    write_junit(directory / "qualification.xml", qualification_nodes())
    sinks = directory / "sinks"
    sinks.mkdir()
    for sample in report.samples:
        if not isinstance(sample, SinkSample):
            continue
        _, _, sink, level = sample.workload.split(".")
        raw = SinkReport.model_validate(
            {
                **sample.model_dump(
                    exclude={
                        "workload",
                        "repeat",
                        "count",
                        "seconds",
                        "us_per_record",
                        "actual_exit_status",
                    }
                ),
                "sink": sink,
                "level": level,
                "completed_refusals": sample.count,
                "elapsed_seconds": sample.seconds,
            }
        )
        stem = sinks / f"{sink}-{level}"
        stem.with_suffix(".stdout").write_text(raw.model_dump_json())
        stem.with_suffix(".stderr").write_bytes(b"")
        write_execution(stem.with_suffix(".execution.json"))
    pressure = directory / "pressure"
    pressure.mkdir()
    (pressure / "cleanup.json").write_text(
        json.dumps({"manifests": 12, "failures": []})
    )
    failures = (
        "controller_control_closed",
        "controller_cancelled",
        "child_deadline",
        "child_failure",
    )
    for index in range(12):
        manifest = PressureManifest(
            version=1,
            platform=report.platform,
            uid=os.geteuid(),
            directory=f"/parser-fixture/{index}",
            directory_stamp=ImageStamp(device=1, inode=100 + index),
            nonce=f"{index:032x}",
            controller=ProcessToken(pid=1000 + index, identity="parser-fixture"),
            lease_stamp=ImageStamp(device=1, inode=200 + index),
            detached=True,
            resources_closed=True,
        )
        for phase in ("before", "after"):
            (pressure / f"{phase}-{index}.manifest.json").write_text(
                manifest.model_dump_json()
            )
        if index == 0:
            result = PressureReport(
                provider_started=True,
                payload_enospc=True,
                metadata_enospc=True,
                payload_errno=28,
                metadata_sqlite_code=13,
                metadata_headroom_bytes=4096,
                unknown_state_charged=True,
                cleanup_completed=True,
                resources_closed=True,
                filesystem="ext4" if report.platform == "linux" else "hfs",
            )
        elif index <= 4:
            result = PressureReport(
                provider_started=True,
                resources_closed=True,
                failure=failures[index - 1],
            )
        else:
            result = PressureReport(resources_closed=True)
        (pressure / f"final-{index}.json").write_text(result.model_dump_json())
        if index < 9:
            (pressure / f"primary-{index}.json").write_text(result.model_dump_json())
