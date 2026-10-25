import json
from pathlib import Path

import pytest

from kora.ir.json import parse_json_object
from tests.measurements.harness import new_report
from tests.measurements.storage_evidence_fixture import (
    write_lifetime,
    write_qualification,
)
from tests.measurements.storage_lifetime_manifest import (
    PRODUCERS,
    WORKLOADS,
    samples,
    storage_report_json,
)
from tests.measurements.storage_lifetime_resources import source_digest
from tests.measurements.storage_measurement_evidence import (
    EvidenceFailure,
    StorageReport,
    validate,
    validate_report,
)
from tests.measurements.test_storage_qualification_evidence import qualification_report

REVISION: str = "a" * 40
DIGEST: str = "b" * 64


def raw_union(directory: Path, *, duplicate_process: bool = False) -> Path:
    repository = Path(__file__).resolve().parents[2]
    digest = source_digest(repository)
    for platform, repeat in sorted(PRODUCERS):
        report = StorageReport.model_validate(report_data(platform, repeat))
        native_repeat = (
            0 if duplicate_process and platform == "linux" and repeat == 1 else repeat
        )
        report = report.model_copy(
            update={
                "environment": report.environment.model_copy(
                    update={
                        "source_digest": digest,
                        "pid": 1234,
                        "boot_session": (
                            f"{native_repeat:08x}-0000-4000-8000-000000000000"
                        ),
                        "process_identity": f"{platform}:native:{native_repeat}",
                    }
                )
            }
        )
        write_lifetime(directory / f"{platform}-{repeat}", report)
    for platform in ("linux", "darwin"):
        report = qualification_report()
        report = report.model_copy(
            update={
                "platform": platform,
                "environment": report.environment.model_copy(
                    update={"source_digest": digest}
                ),
            }
        )
        write_qualification(directory / f"{platform}-qualification", report)
    return repository


def test_storage_raw_union_requires_all_platform_producers_and_qualification(
    tmp_path: Path,
) -> None:
    repository = raw_union(tmp_path)

    result = validate(tmp_path, REVISION, repository)

    assert result["producers"] == 6
    assert result["qualifications"] == 2


def test_storage_raw_union_refuses_reused_native_process_evidence(
    tmp_path: Path,
) -> None:
    repository = raw_union(tmp_path, duplicate_process=True)

    with pytest.raises(EvidenceFailure, match="process identities are duplicated"):
        validate(tmp_path, REVISION, repository)


@pytest.mark.parametrize(
    "defect",
    [
        "missing_producer",
        "failed_junit",
        "checkpoint",
        "sink_field",
        "ownership",
        "native_errno",
    ],
)
def test_storage_raw_union_refuses_corrupted_child_and_ownership_evidence(
    tmp_path: Path, defect: str
) -> None:
    repository = raw_union(tmp_path)
    if defect == "missing_producer":
        (tmp_path / "linux-2/report.json").unlink()
    elif defect == "failed_junit":
        (tmp_path / "linux-2/measurement.xml").write_text(
            '<testsuite><testcase classname="storage" name="failed">'
            "<failure/></testcase></testsuite>"
        )
    elif defect == "checkpoint":
        path = tmp_path / "linux-2/rows.bin"
        encoded = path.read_bytes()
        path.write_bytes(encoded[:-1])
    else:
        path = (
            tmp_path
            / "linux-qualification"
            / {
                "sink_field": "sinks/blocked-INFO.stdout",
                "ownership": "pressure/after-0.manifest.json",
                "native_errno": "pressure/primary-0.json",
            }[defect]
        )
        data = parse_json_object(json.loads(path.read_text()))
        field, value = {
            "sink_field": ("payload_verified", False),
            "ownership": ("nonce", "f" * 32),
            "native_errno": ("payload_errno", 5),
        }[defect]
        data[field] = value
        path.write_text(json.dumps(data))

    with pytest.raises(EvidenceFailure):
        validate(tmp_path, REVISION, repository)


def report_data(platform: str = "linux", repeat: int = 0) -> dict[str, object]:
    report = new_report()
    report.platform = platform
    report.revision = REVISION
    report.environment.update(
        producer=repeat,
        pid=1234,
        boot_session="00000000-0000-4000-8000-000000000000",
        process_identity=f"{platform}:native:{repeat}",
        source_digest=DIGEST,
        installed_source="src/kora/plugins/storage/local.py",
        checkout_clean=True,
        warmup_cycles=1,
        small_cycles=100,
        small_raw_includes_prefix=True,
        maximum_cycles=3,
        rss_method="proc_statm" if platform == "linux" else "reaped_ps_kib",
        maxrss_meaning="cumulative_process_high_water",
        sampler_children_reaped=True,
        tracemalloc_nframe=1,
        numeric_capture="preallocated_array_d_report_population_after_tracing",
        timing="complete_cycle_including_verification_excluding_phase_sampling",
        unlinked_scope="all_process_regular_fds_on_root_device",
        filesystem="ext4" if platform == "linux" else "apfs",
        checkpoint_format="fixed_rows_v1",
        checkpoint_descriptors=1,
        load_context="github_hosted_runner",
    )
    report.samples = samples(repeat)
    for sample in report.samples:
        sample.seconds = 0.1 if sample.extra["phase"] == "cycle" else 0
        sample.extra["executed"] = True
        sample.extra["gross_seconds"] = sample.seconds
        cycle = sample.extra["cycle"]
        assert isinstance(cycle, int)
        point = (
            cycle == 0
            or cycle % 10 == 0
            or sample.workload.endswith((".16777216", ".268435456"))
        )
        sample.extra["resource_sample"] = point
        if point:
            for name in (
                "phase_peak_rss_bytes",
                "phase_peak_unique_bytes",
                "phase_peak_allocated_bytes",
                "python_current_bytes",
                "python_post_gc_bytes",
                "python_peak_bytes",
                "tracer_bytes",
                "rss_bytes",
                "cumulative_maxrss_bytes",
                "database_bytes",
            ):
                sample.extra[name] = 4096
    report.gates = dict.fromkeys(
        (*[workload.name for workload in WORKLOADS], "complete"), True
    )
    report.durations["complete"] = 30
    data: object = json.loads(storage_report_json(report))
    return dict[str, object](parse_json_object(data))


@pytest.mark.parametrize(
    "field",
    [
        "phase_peak_rss_bytes",
        "phase_peak_unique_bytes",
        "phase_peak_allocated_bytes",
        "python_current_bytes",
        "python_post_gc_bytes",
        "tracer_bytes",
    ],
)
def test_storage_evidence_refuses_unavailable_measured_resource_counters(
    field: str,
) -> None:
    report = StorageReport.model_validate(report_data())
    changed = report.samples[10].model_copy(update={field: 0})
    report = report.model_copy(
        update={"samples": (*report.samples[:10], changed, *report.samples[11:])}
    )

    with pytest.raises(EvidenceFailure, match="counter is unavailable"):
        validate_report(report, REVISION, DIGEST)


def test_storage_evidence_refuses_excessive_settled_python_memory() -> None:
    report = StorageReport.model_validate(report_data())
    changed = report.samples[10].model_copy(update={"python_post_gc_bytes": 16777216})
    report = report.model_copy(
        update={"samples": (*report.samples[:10], changed, *report.samples[11:])}
    )

    with pytest.raises(EvidenceFailure, match="budget"):
        validate_report(report, REVISION, DIGEST)


@pytest.mark.parametrize(
    "field", ["python_current_bytes", "python_post_gc_bytes", "tracer_bytes"]
)
def test_storage_evidence_refuses_unavailable_warmed_baseline(field: str) -> None:
    report = StorageReport.model_validate(report_data())
    baseline = report.samples[0].model_copy(update={field: 0})
    report = report.model_copy(update={"samples": (baseline, *report.samples[1:])})

    with pytest.raises(EvidenceFailure, match="counter is unavailable"):
        validate_report(report, REVISION, DIGEST)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_storage_evidence_accepts_declared_producer_independent_of_gate_platform(
    platform: str,
) -> None:
    report = StorageReport.model_validate(report_data(platform))

    validate_report(report, REVISION, DIGEST)

    assert report.platform == platform
    assert {sample.repeat for sample in report.samples} == {0}


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "duplicate",
        "failed",
        "revision",
        "source",
        "producer",
        "rss",
        "resources",
        "derived_timing",
        "sample_order",
    ],
)
def test_storage_evidence_refuses_incomplete_or_invalid_reports(defect: str) -> None:
    data = report_data()
    report = StorageReport.model_validate(data)
    first = report.samples[0]
    if defect == "missing":
        changed = report.model_copy(update={"samples": report.samples[1:]})
    elif defect == "duplicate":
        changed = report.model_copy(update={"samples": (*report.samples, first)})
    elif defect == "failed":
        changed = report.model_copy(
            update={"gates": {**report.gates, "complete": False}}
        )
    elif defect == "revision":
        changed = report.model_copy(update={"revision": "c" * 40})
    elif defect == "source":
        changed = report.model_copy(
            update={
                "environment": report.environment.model_copy(
                    update={"source_digest": "c" * 64}
                )
            }
        )
    elif defect == "sample_order":
        changed = report.model_copy(
            update={
                "samples": (report.samples[1], report.samples[0], *report.samples[2:])
            }
        )
    elif defect == "derived_timing":
        changed = report.model_copy(
            update={
                "samples": tuple(
                    sample.model_copy(update={"us_per_record": 0.0})
                    for sample in report.samples
                ),
                "summary": {
                    name: {
                        **values,
                        "median_us_per_record": 0.0,
                        "p95_us_per_record": 0.0,
                    }
                    for name, values in report.summary.items()
                },
            }
        )
    elif defect == "producer":
        changed = report.model_copy(
            update={
                "samples": (first.model_copy(update={"repeat": 2}), *report.samples[1:])
            }
        )
    elif defect == "rss":
        changed = report.model_copy(
            update={
                "samples": (
                    first.model_copy(update={"rss_bytes": 0}),
                    *report.samples[1:],
                )
            }
        )
    else:
        changed = report.model_copy(
            update={
                "samples": (
                    first.model_copy(update={"receipts": 1}),
                    *report.samples[1:],
                )
            }
        )

    with pytest.raises(EvidenceFailure):
        validate_report(changed, REVISION, DIGEST)


@pytest.mark.parametrize("outcome", ["failure", "error", "skipped"])
def test_storage_junit_rejects_unsuccessful_measurement(
    tmp_path: Path, outcome: str
) -> None:
    from tests.measurements.storage_measurement_evidence import validate_junit

    path = tmp_path / "measurement.xml"
    path.write_text(
        "<testsuites><testsuite>"
        '<testcase name="test_artifact_storage_lifetime[0]">'
        f"<{outcome}/></testcase></testsuite></testsuites>"
    )

    with pytest.raises(EvidenceFailure):
        validate_junit(path, 0)


def test_storage_evidence_requires_all_six_independent_producers(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]

    with pytest.raises(EvidenceFailure, match="union is incomplete"):
        validate(tmp_path, REVISION, root)
