import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kora.ir.json import JsonObject, parse_json_object
from tests.measurements.storage_budgets import (
    BUDGET_PATH,
    GrowthBudget,
    read_budgets,
    sampled_payload_bytes,
    validate_growth,
)
from tests.measurements.storage_evidence_common import EvidenceFailure
from tests.measurements.storage_lifetime_manifest import Workload
from tests.measurements.storage_measurement_evidence import (
    StorageReport,
    validate_report,
)
from tests.measurements.test_storage_measurement_evidence import (
    DIGEST,
    REVISION,
    report_data,
)


def config_data() -> JsonObject:
    return parse_json_object(json.loads(BUDGET_PATH.read_text()))


@pytest.mark.parametrize(
    "defect",
    [
        "version_bool",
        "version_unknown",
        "missing_platform",
        "extra_platform",
        "missing_workload",
        "extra_workload",
        "missing_bound",
        "extra_bound",
        "boolean_bound",
        "string_bound",
        "negative_bound",
        "null_bound",
        "missing_maximum",
        "inapplicable_maximum",
        "null_maximum",
    ],
)
def test_storage_budget_configuration_refuses_incomplete_or_untyped_policy(
    tmp_path: Path, defect: str
) -> None:
    data = config_data()
    platforms = parse_json_object(data["platforms"])
    linux = parse_json_object(platforms["linux"])
    workloads = parse_json_object(linux["workloads"])
    maximum = parse_json_object(workloads["storage.raw.16777216"])
    growth = parse_json_object(linux["python_post_gc_bytes"])
    if defect == "version_bool":
        data["version"] = True
    elif defect == "version_unknown":
        data["version"] = 2
    elif defect == "missing_platform":
        del platforms["darwin"]
    elif defect == "extra_platform":
        platforms["unsupported"] = linux
    elif defect == "missing_workload":
        del workloads["storage.raw.65536"]
    elif defect == "extra_workload":
        workloads["unknown"] = maximum
    elif defect == "missing_bound":
        del growth["absolute"]
    elif defect == "extra_bound":
        growth["unknown"] = 1
    elif defect == "boolean_bound":
        growth["absolute"] = True
    elif defect == "string_bound":
        growth["absolute"] = "100"
    elif defect == "negative_bound":
        growth["absolute"] = -1
    elif defect == "null_bound":
        growth["absolute"] = None
    elif defect == "missing_maximum":
        del maximum["maximum_cycle_seconds"]
    elif defect == "inapplicable_maximum":
        small = parse_json_object(workloads["storage.raw.65536"])
        small["maximum_cycle_seconds"] = 1.0
        workloads["storage.raw.65536"] = small
    else:
        maximum["maximum_cycle_seconds"] = None
    linux["python_post_gc_bytes"] = growth
    workloads["storage.raw.16777216"] = maximum
    linux["workloads"] = workloads
    platforms["linux"] = linux
    data["platforms"] = platforms
    path = tmp_path / "budgets.json"
    path.write_text(json.dumps(data))

    with pytest.raises(ValidationError):
        read_budgets(path)


@pytest.mark.parametrize(
    "encoded",
    [
        '{"version":1,"version":1}',
        '{"version":NaN}',
        '{"version":Infinity}',
        '{"version":1e999}',
        "[]",
    ],
)
def test_storage_budget_configuration_refuses_ambiguous_or_nonfinite_json(
    tmp_path: Path, encoded: str
) -> None:
    path = tmp_path / "budgets.json"
    path.write_text(encoded)

    with pytest.raises((ValidationError, EvidenceFailure)):
        read_budgets(path)


def test_storage_budget_configuration_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "budgets.json"
    path.write_bytes(b" " * 32769)

    with pytest.raises(EvidenceFailure, match="input bound"):
        read_budgets(path)


def test_growth_budget_accepts_each_inclusive_boundary() -> None:
    budget = GrowthBudget(
        absolute=100, warmed_growth=10, late_endpoint_growth=3, late_excursion_growth=5
    )
    values = ((0, 90), (50, 95), (60, 100), (100, 98))

    validate_growth(values, 90, budget, "retained")

    assert values[-1] == (100, 98)


def test_linux_allocated_growth_accepts_the_approved_late_boundaries() -> None:
    budget = read_budgets().platforms["linux"].allocated_bytes
    values = ((0, 65536), (50, 61440), (60, 69632), (100, 69632))

    validate_growth(values, 65536, budget, "allocated_bytes")

    assert budget.absolute == 81920
    assert budget.warmed_growth == 8192
    assert budget.late_endpoint_growth == 8192
    assert budget.late_excursion_growth == 8192


@pytest.mark.parametrize(
    ("peak", "endpoint", "metric"),
    [(69633, 61440, "late_excursion_growth"), (69633, 69633, "late_endpoint_growth")],
)
def test_linux_allocated_growth_refuses_an_excessive_excursion_or_endpoint(
    peak: int, endpoint: int, metric: str
) -> None:
    budget = read_budgets().platforms["linux"].allocated_bytes
    values = ((0, 65536), (50, 61440), (60, peak), (100, endpoint))

    with pytest.raises(EvidenceFailure, match=metric):
        validate_growth(values, 65536, budget, "allocated_bytes")


@pytest.mark.parametrize(
    ("budget", "values", "metric"),
    [
        (
            GrowthBudget(
                absolute=99,
                warmed_growth=100,
                late_endpoint_growth=100,
                late_excursion_growth=100,
            ),
            ((0, 90), (50, 95), (60, 100), (100, 98)),
            "retained:",
        ),
        (
            GrowthBudget(
                absolute=100,
                warmed_growth=9,
                late_endpoint_growth=100,
                late_excursion_growth=100,
            ),
            ((0, 90), (50, 95), (60, 100), (100, 98)),
            "warmed_growth",
        ),
        (
            GrowthBudget(
                absolute=100,
                warmed_growth=100,
                late_endpoint_growth=2,
                late_excursion_growth=100,
            ),
            ((0, 90), (50, 95), (60, 100), (100, 98)),
            "late_endpoint_growth",
        ),
        (
            GrowthBudget(
                absolute=100,
                warmed_growth=100,
                late_endpoint_growth=3,
                late_excursion_growth=4,
            ),
            ((0, 90), (50, 95), (60, 100), (100, 98)),
            "late_excursion_growth",
        ),
    ],
)
def test_growth_budget_refuses_one_exceeded_boundary(
    budget: GrowthBudget, values: tuple[tuple[int, int], ...], metric: str
) -> None:
    with pytest.raises(EvidenceFailure, match=metric):
        validate_growth(values, 90, budget, "retained")


def test_three_cycle_growth_omits_only_the_inapplicable_late_window() -> None:
    budget = GrowthBudget(
        absolute=100, warmed_growth=10, late_endpoint_growth=0, late_excursion_growth=0
    )

    validate_growth(((0, 90), (1, 95), (2, 100), (3, 98)), 90, budget, "retained")

    with pytest.raises(EvidenceFailure, match="warmed_growth"):
        validate_growth(((0, 89), (1, 95), (2, 100), (3, 98)), 89, budget, "retained")


@pytest.mark.parametrize(
    ("ending", "size", "expected"),
    [
        ("raw", 65536, 196608),
        ("raw", 16777216, 33554432),
        ("prepared", 65536, 131073),
        ("prepared", 268435456, 536870911),
        ("alias", 65536, 131072),
        ("source_error", 65536, 65536),
        ("cancelled", 65536, 65536),
        ("lost_response", 0, 0),
        ("expired", 1, 2),
    ],
)
def test_sampled_payload_term_follows_the_declared_trace(
    ending: str, size: int, expected: int
) -> None:
    workload = Workload(ending, size, 100 if size <= 65536 else 3)

    payload = sampled_payload_bytes(workload)

    assert payload == expected


@pytest.mark.parametrize("metric", ["legacy_p95_seconds", "maximum_cycle_seconds"])
def test_cycle_timing_outlier_is_reported_without_blocking_qualification(
    metric: str,
) -> None:
    report = StorageReport.model_validate(report_data())
    name = "storage.raw.16777216"
    changed = tuple(
        sample.model_copy(
            update={"seconds": 2.0, "gross_seconds": 2.0, "us_per_record": 2000000.0}
        )
        if sample.workload == name
        and sample.phase == "cycle"
        and (metric == "legacy_p95_seconds" or sample.cycle == 3)
        else sample
        for sample in report.samples
    )
    report = report.model_copy(update={"samples": changed})
    if metric == "legacy_p95_seconds":
        report = report.model_copy(
            update={
                "summary": {
                    **report.summary,
                    name: {
                        "median_us_per_record": 2000000.0,
                        "p95_us_per_record": 2000000.0,
                        "repeats": 3,
                    },
                }
            }
        )
    timing = read_budgets().platforms["linux"].workloads[name]
    reference = (
        timing.legacy_p95_seconds
        if metric == "legacy_p95_seconds"
        else timing.maximum_cycle_seconds
    )

    diagnostics = validate_report(report, REVISION, DIGEST)

    assert {
        "metric": name + "." + metric,
        "observed": 2.0,
        "reference": reference,
        "exceeds_reference": True,
    } in diagnostics


def test_baseline_python_peak_does_not_inherit_a_measured_cycle_budget() -> None:
    report = StorageReport.model_validate(report_data())
    baseline = report.samples[0].model_copy(update={"python_peak_bytes": 16777216})
    report = report.model_copy(update={"samples": (baseline, *report.samples[1:])})

    validate_report(report, REVISION, DIGEST)

    assert baseline.phase == "baseline"


@pytest.mark.parametrize(
    ("cycle", "metric"), [(60, "late_excursion_growth"), (100, "late_endpoint_growth")]
)
def test_storage_diagnostics_keep_late_retention_separate_from_absolute_allowance(
    cycle: int, metric: str
) -> None:
    report = StorageReport.model_validate(report_data())
    changed = tuple(
        sample.model_copy(
            update={"python_post_gc_bytes": 88000 if sample.cycle == cycle else 80000}
        )
        if sample.workload == "storage.raw.65536" and sample.resource_sample
        else sample
        for sample in report.samples
    )
    report = report.model_copy(update={"samples": changed})
    growth = read_budgets().platforms["linux"].python_post_gc_bytes

    diagnostics = validate_report(report, REVISION, DIGEST)

    assert {
        "metric": "storage.raw.65536.python_post_gc_bytes." + metric,
        "observed": 8000,
        "reference": growth.late_excursion_growth
        if metric == "late_excursion_growth"
        else growth.late_endpoint_growth,
        "exceeds_reference": True,
    } in diagnostics


@pytest.mark.parametrize(
    ("field", "maximum"),
    [
        ("phase_peak_unique_bytes", 35651584),
        ("phase_peak_allocated_bytes", 35651584),
        ("python_peak_bytes", 16777216),
        ("python_current_bytes", 16777216),
        ("database_bytes", 1048576),
        ("journal_bytes", 1),
        ("unique_bytes", 1048576),
        ("allocated_bytes", 1048576),
    ],
)
def test_storage_resource_budget_refuses_each_exceeded_metric(
    field: str, maximum: int
) -> None:
    report = StorageReport.model_validate(report_data())
    changed = tuple(
        sample.model_copy(update={field: maximum})
        if sample.workload == "storage.raw.16777216" and sample.cycle == 1
        else sample
        for sample in report.samples
    )
    report = report.model_copy(update={"samples": changed})

    with pytest.raises(EvidenceFailure, match="budget exceeded"):
        validate_report(report, REVISION, DIGEST)


@pytest.mark.parametrize(
    ("field", "observed"),
    [
        ("python_post_gc_bytes", 16777216),
        ("tracer_bytes", 16777216),
        ("rss_bytes", 1073741824),
        ("cumulative_maxrss_bytes", 1073741824),
        ("phase_peak_rss_bytes", 1073741824),
    ],
)
def test_storage_process_memory_outliers_remain_visible_diagnostics(
    field: str, observed: int
) -> None:
    report = StorageReport.model_validate(report_data())
    changed = tuple(
        sample.model_copy(update={field: observed})
        if sample.workload == "storage.raw.16777216" and sample.cycle == 1
        else sample
        for sample in report.samples
    )
    report = report.model_copy(update={"samples": changed})
    growth: object = getattr(read_budgets().platforms["linux"], field)
    if not isinstance(growth, GrowthBudget):
        raise TypeError("the diagnostic field has no growth reference")

    diagnostics = validate_report(report, REVISION, DIGEST)

    assert {
        "metric": "storage.raw.16777216." + field,
        "observed": observed,
        "reference": growth.absolute,
        "exceeds_reference": True,
    } in diagnostics


@pytest.mark.parametrize(
    "field", ["phase_peak_unique_bytes", "phase_peak_allocated_bytes"]
)
def test_storage_phase_budget_charges_payload_separately_from_overhead(
    field: str,
) -> None:
    report = StorageReport.model_validate(report_data())
    budget = read_budgets().platforms["linux"]
    overhead = (
        budget.phase_unique_overhead_bytes
        if field == "phase_peak_unique_bytes"
        else budget.phase_allocated_overhead_bytes
    )
    changed = tuple(
        sample.model_copy(update={field: 196608 + overhead})
        if sample.workload == "storage.raw.65536" and sample.cycle == 10
        else sample
        for sample in report.samples
    )
    report = report.model_copy(update={"samples": changed})

    validate_report(report, REVISION, DIGEST)
    exceeded = tuple(
        sample.model_copy(update={field: 196609 + overhead})
        if sample.workload == "storage.raw.65536" and sample.cycle == 10
        else sample
        for sample in report.samples
    )

    with pytest.raises(EvidenceFailure, match="budget exceeded"):
        validate_report(
            report.model_copy(update={"samples": exceeded}), REVISION, DIGEST
        )
