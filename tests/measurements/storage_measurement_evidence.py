import json
import statistics
import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr

from kora.ir.json import JsonValue
from kora.protocols.common import BoundaryModel
from tests.measurements.storage_checkpoint import read_checkpoint
from tests.measurements.storage_evidence_common import (
    EvidenceFailure,
    junit_nodes,
    read_json,
    validate_execution,
)
from tests.measurements.storage_lifetime_manifest import (
    PRODUCERS,
    RESOURCE_FIELDS,
    TIMING_FIELDS,
    WORKLOADS,
    samples,
)
from tests.measurements.storage_lifetime_resources import source_digest
from tests.measurements.storage_qualification_evidence import validate_qualification

Nonnegative = Annotated[StrictInt, Field(ge=0)]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class Environment(BoundaryModel):
    producer: Annotated[StrictInt, Field(ge=0, le=2)]
    pid: Annotated[StrictInt, Field(ge=1)]
    boot_session: Annotated[
        StrictStr,
        Field(
            pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"
        ),
    ]
    process_identity: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    source_digest: Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]
    installed_source: Literal["src/kora/plugins/storage/local.py"]
    checkout_clean: Literal[True]
    warmup_cycles: Literal[1]
    small_cycles: Literal[100]
    small_raw_includes_prefix: Literal[True]
    maximum_cycles: Literal[3]
    rss_method: Literal["proc_statm", "reaped_ps_kib"]
    maxrss_meaning: Literal["cumulative_process_high_water"]
    sampler_children_reaped: Literal[True]
    tracemalloc_nframe: Literal[1]
    numeric_capture: Literal["preallocated_array_d_report_population_after_tracing"]
    timing: Literal["complete_cycle_including_verification_excluding_phase_sampling"]
    unlinked_scope: Literal["all_process_regular_fds_on_root_device"]
    load_context: Literal[
        "shared_local_sessions", "github_hosted_runner", "local_unspecified"
    ]
    filesystem: Literal["ext4", "apfs", "hfs"]
    checkpoint_format: Literal["fixed_rows_v1"]
    checkpoint_descriptors: Literal[1]


class StorageSample(BoundaryModel):
    workload: StrictStr
    repeat: Annotated[StrictInt, Field(ge=0, le=2)]
    count: Literal[1]
    seconds: Seconds
    us_per_record: Seconds
    cycle: Nonnegative
    phase: Literal["baseline", "cycle"]
    resource_sample: StrictBool
    executed: StrictBool
    gross_seconds: Seconds
    phase_observer_seconds: Seconds
    control_observer_seconds: Seconds
    phase_peak_rss_bytes: Nonnegative
    phase_peak_unique_bytes: Nonnegative
    phase_peak_allocated_bytes: Nonnegative
    python_current_bytes: Nonnegative
    python_post_gc_bytes: Nonnegative
    python_peak_bytes: Nonnegative
    tracer_bytes: Nonnegative
    rss_bytes: Nonnegative
    cumulative_maxrss_bytes: Nonnegative
    threads: Nonnegative
    descriptors: Nonnegative
    tasks: Nonnegative
    owned_jobs: Nonnegative
    native_jobs: Nonnegative
    readers: Nonnegative
    connections: Nonnegative
    stage_rows: Nonnegative
    delete_rows: Nonnegative
    receipts: Nonnegative
    content_rows: Nonnegative
    reserved_bytes: Nonnegative
    database_bytes: Nonnegative
    journal_bytes: Nonnegative
    unique_bytes: Nonnegative
    allocated_bytes: Nonnegative
    private_files: Nonnegative
    payload_links: Nonnegative
    unlinked_bytes: Nonnegative


class StorageReport(BoundaryModel):
    started_at: StrictStr
    platform: Literal["linux", "darwin"]
    python: StrictStr
    revision: StrictStr
    environment: Environment
    summary: dict[str, dict[str, float]]
    gates: dict[str, StrictBool]
    durations_seconds: dict[str, Seconds]
    samples: tuple[StorageSample, ...]


def read_report(path: Path) -> StorageReport:
    return StorageReport.model_validate(read_json(path))


def validate_report(report: StorageReport, revision: str, digest: str) -> None:
    if report.revision != revision or report.environment.source_digest != digest:
        raise EvidenceFailure("the storage evidence source/revision does not match")
    expected_gates = {workload.name for workload in WORKLOADS} | {"complete"}
    if set(report.gates) != expected_gates or not all(report.gates.values()):
        raise EvidenceFailure("a required storage workload failed or did not run")
    if report.durations_seconds.keys() != {"complete"}:
        raise EvidenceFailure("the storage run duration is missing")
    platform = report.platform
    environment = report.environment
    if (
        platform == "linux"
        and (environment.filesystem != "ext4" or environment.rss_method != "proc_statm")
    ) or (
        platform == "darwin"
        and (
            environment.filesystem not in {"apfs", "hfs"}
            or environment.rss_method != "reaped_ps_kib"
        )
    ):
        raise EvidenceFailure(
            "the declared producer filesystem/counter is inconsistent"
        )
    expected = {
        (workload.name, cycle, "baseline" if cycle == 0 else "cycle")
        for workload in WORKLOADS
        for cycle in range(workload.cycles + 1)
    }
    actual = {
        (sample.workload, sample.cycle, sample.phase) for sample in report.samples
    }
    if actual != expected or len(actual) != len(report.samples):
        raise EvidenceFailure("the exact storage sample union is missing or duplicated")
    declared = samples(environment.producer)
    if [(sample.workload, sample.cycle, sample.phase) for sample in report.samples] != [
        (sample.workload, sample.extra["cycle"], sample.extra["phase"])
        for sample in declared
    ]:
        raise EvidenceFailure(
            "the ordered storage sample inventory differs from its manifest"
        )
    for sample in report.samples:
        if not sample.executed:
            raise EvidenceFailure("a required storage cycle was not executed")
        if abs(sample.us_per_record - sample.seconds / sample.count * 1000000) > 1e-6:
            raise EvidenceFailure("the derived storage timing differs from raw seconds")
        if (
            abs(
                sample.seconds
                - (
                    sample.gross_seconds
                    - sample.phase_observer_seconds
                    - sample.control_observer_seconds
                )
            )
            > 1e-9
        ):
            raise EvidenceFailure("storage timing subtraction is inconsistent")
        if sample.repeat != environment.producer:
            raise EvidenceFailure("a sample belongs to a different independent process")
        maximum = sample.workload.endswith((".16777216", ".268435456"))
        point = sample.cycle == 0 or sample.cycle % 10 == 0 or maximum
        if point != sample.resource_sample:
            raise EvidenceFailure("a required quiescent resource sample is missing")
        if sample.phase == "cycle" and sample.seconds <= 0:
            raise EvidenceFailure("a measured storage cycle has no elapsed time")
        if any(
            getattr(sample, name) != 0
            for name in (
                "owned_jobs",
                "native_jobs",
                "readers",
                "connections",
                "stage_rows",
                "delete_rows",
                "receipts",
                "content_rows",
                "reserved_bytes",
            )
        ):
            raise EvidenceFailure("settled ownership or disposable state remains")
        if point:
            if (
                min(
                    sample.python_peak_bytes,
                    sample.rss_bytes,
                    sample.cumulative_maxrss_bytes,
                    sample.database_bytes,
                )
                <= 0
            ):
                raise EvidenceFailure(
                    "a required native/Python/disk counter is unavailable"
                )
            if sample.private_files or sample.payload_links or sample.unlinked_bytes:
                raise EvidenceFailure("settled payload resources remain")
    if set(RESOURCE_FIELDS) - set(StorageSample.model_fields):
        raise EvidenceFailure("the evidence schema omits a declared resource")
    if set(report.summary) != {workload.name for workload in WORKLOADS}:
        raise EvidenceFailure("the timing summary workload union is incomplete")
    for workload in WORKLOADS:
        timings = sorted(
            sample.seconds / sample.count * 1000000
            for sample in report.samples
            if sample.workload == workload.name and sample.phase == "cycle"
        )
        expected_summary = {
            "median_us_per_record": statistics.median(timings),
            "p95_us_per_record": timings[max(0, int(len(timings) * 0.95) - 1)],
            "repeats": len(timings),
        }
        if report.summary[workload.name] != expected_summary:
            raise EvidenceFailure(
                "the storage timing summary includes non-cycle samples "
                "or differs from raw evidence"
            )


def validate_junit(path: Path, repeat: int) -> None:
    expected = {
        f"tests.measurements.test_artifact_storage_lifetime.test_artifact_storage_lifetime[{repeat}]"
    }
    if set(junit_nodes(path)) != expected:
        raise EvidenceFailure(
            "the exact storage test inventory is missing or duplicated"
        )


def validate_checkpoint(producer: Path, report: StorageReport) -> None:
    header, rows, tail = read_checkpoint(producer)
    if (
        tail
        or len(rows) != len(report.samples)
        or header.revision != report.revision
        or header.repeat != report.environment.producer
        or header.platform != report.platform
        or header.python != report.python
        or header.started_at != report.started_at
        or header.environment != report.environment.model_dump(mode="json")
    ):
        raise EvidenceFailure("the checkpoint provenance or complete row union differs")
    for row, sample in zip(rows, report.samples, strict=True):
        expected = (
            sample.seconds,
            *(float(getattr(sample, name)) for name in RESOURCE_FIELDS),
            *(float(getattr(sample, name)) for name in TIMING_FIELDS),
        )
        if (
            row.executed != sample.executed
            or row.captured != sample.resource_sample
            or row.values != expected
        ):
            raise EvidenceFailure(
                "the checkpoint numeric values differ from the report"
            )


def validate(root: Path, revision: str, repository: Path) -> dict[str, JsonValue]:
    reports = tuple(root.rglob("report.json"))
    seen: set[tuple[str, int]] = set()
    processes: set[tuple[str, str, int, str]] = set()
    digest = source_digest(repository)
    for path in reports:
        report = read_report(path)
        identity = (report.platform, report.environment.producer)
        if identity not in PRODUCERS or identity in seen:
            raise EvidenceFailure(
                "an unexpected or duplicate storage producer was found"
            )
        seen.add(identity)
        process = (
            report.platform,
            report.environment.boot_session,
            report.environment.pid,
            report.environment.process_identity,
        )
        if process in processes:
            raise EvidenceFailure(
                "independent producer process identities are duplicated"
            )
        processes.add(process)
        validate_report(report, revision, digest)
        producer = path.parent
        validate_checkpoint(producer, report)
        validate_junit(producer / "measurement.xml", report.environment.producer)
        validate_execution(producer / "execution.json", report.environment.pid)
        for stream in ("stdout.txt", "stderr.txt"):
            if (producer / stream).read_bytes():
                raise EvidenceFailure("the measurement child emitted unexpected output")
        for workload in WORKLOADS:
            if workload.cycles == 100:
                for cycle in (50, 100):
                    for suffix in ("snapshot", "txt"):
                        allocation = producer / f"{workload.name}-{cycle}.{suffix}"
                        if allocation.stat().st_size == 0:
                            raise EvidenceFailure("allocation evidence is empty")
    if seen != set(PRODUCERS):
        raise EvidenceFailure(
            "the Linux/macOS independent-producer union is incomplete"
        )
    qualifications: set[str] = set()
    for path in root.rglob("boundaries.json"):
        platform = validate_qualification(path.parent, revision, digest)
        if platform in qualifications:
            raise EvidenceFailure("a qualification platform is duplicated")
        qualifications.add(platform)
    if qualifications != {"linux", "darwin"}:
        raise EvidenceFailure("the platform qualification union is incomplete")
    return {
        "revision": revision,
        "producers": len(seen),
        "qualifications": len(qualifications),
        "source_digest": digest,
    }


if __name__ == "__main__":
    sys.stdout.write(
        json.dumps(validate(Path(sys.argv[1]), sys.argv[2], Path.cwd()), sort_keys=True)
        + "\n"
    )
