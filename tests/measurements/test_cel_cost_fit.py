import json
from dataclasses import replace
from fractions import Fraction

import pytest

from kora.cel import regex
from tests.measurements.cel_bound_workload import BOUND_FAMILIES
from tests.measurements.cel_cost_calibration import CostFamilyReport, mapping, sequence
from tests.measurements.cel_cost_catalogue import (
    MEMORY_IDENTITIES,
    OPERATION_IDENTITIES,
    cost_family_cases,
    memory_case,
)
from tests.measurements.cel_cost_fit import (
    FitJob,
    JobIdentity,
    combined_conversion,
    fit_jobs,
    fit_memory,
    fit_pattern_table,
    fit_series,
    frozen_reference,
    integer,
    memory_delta,
    pattern_cap,
    regex_logical_memory,
    validate_jobs,
    validate_pattern_envelopes,
)
from tests.measurements.cel_cost_workload import logical_memory
from tests.measurements.cel_regex_workload import (
    PATTERN_FORMS,
    REGEX_IDENTITIES,
    REGEX_MEMORY_IDENTITIES,
    BoundaryAttempt,
    BoundaryReport,
)


def bracket(elapsed: int, reference: int = 10) -> dict[str, object]:
    return {
        "elapsed_ns": elapsed,
        "calls": 1,
        "result_count": 1,
        "reference_before_ns": reference,
        "reference_after_ns": reference,
        "reference_calls": 1,
        "reference_before_results": 1,
        "reference_after_results": 1,
    }


def observation(target: int, control: int) -> dict[str, object]:
    return {
        "target": [bracket(target) for _ in range(18)],
        "control_samples": [bracket(control) for _ in range(18)],
    }


def provisional_table() -> dict[str, int]:
    return {
        "literal": regex.LITERAL_WEIGHT,
        "caseless_literal": regex.CASELESS_LITERAL_WEIGHT,
        "range": regex.RANGE_WEIGHT,
        "caseless_range": regex.CASELESS_RANGE_WEIGHT,
        "any": regex.ANY_WEIGHT,
        "anchor": regex.ANCHOR_WEIGHT,
        **{"class:" + key: value for key, value in regex.CLASS_WEIGHTS.items()},
        **{
            "unicode:" + key: value
            for key, value in regex.UNICODE_CLASS_WEIGHTS.items()
        },
    }


@pytest.mark.parametrize(("family", "size"), REGEX_MEMORY_IDENTITIES)
def test_closed_regex_model_matches_production(family: str, size: int) -> None:
    case = memory_case(family, size)
    table = provisional_table()
    checked = case.checked()
    expected = logical_memory(case, checked)

    actual = regex_logical_memory(
        family,
        size,
        table,
        regex.PATTERN_BASE_WEIGHT,
        regex.SYNTAX_CHARACTER_WEIGHT,
        regex.REPETITION_OVERHEAD,
    )

    assert actual == expected


def test_conversion_preserves_signed_phase_and_normalizes_streams() -> None:
    preparation = observation(9, 10)
    native = observation(14, 10)

    result = combined_conversion(preparation, native, Fraction(20))

    assert result["prepare"] == [Fraction(-2)] * 18
    assert result["native"] == [Fraction(8)] * 18
    assert result["combined"] == [Fraction(6)] * 18


@pytest.mark.parametrize(("target", "control"), [(10, 10), (9, 10)])
def test_conversion_rejects_nonpositive_native_batches(
    target: int, control: int
) -> None:
    preparation = observation(100, 1)
    native = observation(target, control)

    with pytest.raises(ValueError, match="raw native"):
        combined_conversion(preparation, native, Fraction(10))


def test_conversion_rejects_incomplete_pairs() -> None:
    preparation = observation(20, 10)
    native = {"target": [bracket(20)], "control_samples": [bracket(10)]}

    with pytest.raises(ValueError, match="incomplete"):
        combined_conversion(preparation, native, Fraction(10))


def test_series_uses_declared_sizes_for_complexity_and_exact_unit_envelope() -> None:
    sizes = [2, 4, 8, 16]
    units = [16, 48, 112, 240]
    residuals = [[Fraction(size, 3)] * 18 for size in sizes]

    result = fit_series(sizes, units, residuals, quadratic=False)

    assert result["slope_ns"] == Fraction(1, 24)
    assert result["weight_fs"] == 62_500
    assert result["complexity"]["within_limits"] is True


def test_series_does_not_discard_one_negative_retained_value() -> None:
    residuals = [[Fraction(size)] * 18 for size in (2, 4, 8, 16)]
    residuals[0][-1] = Fraction(-1)

    with pytest.raises(ValueError, match="identifiable"):
        fit_series([2, 4, 8, 16], [2, 4, 8, 16], residuals, quadratic=False)


def test_series_rejects_superlinear_series() -> None:
    sizes = [2, 4, 8, 16]
    residuals = [[Fraction(size * size)] * 18 for size in sizes]

    with pytest.raises(ValueError, match="complexity"):
        fit_series(sizes, sizes, residuals, quadratic=False)


def test_memory_delta_uses_reset_current() -> None:
    report: dict[str, object] = {
        "linux_peak_reset": True,
        "baseline": {"current": 100, "peak": 300},
        "reset": {"current": 120, "peak": 180},
        "measured": {"current": 140, "peak": 200},
    }

    result = memory_delta(report)

    assert result == 80


@pytest.mark.parametrize(
    ("reset", "measured"),
    [
        ({"current": 200, "peak": 100}, {"current": 150, "peak": 250}),
        ({"current": 100, "peak": 200}, {"current": 100, "peak": 150}),
        ({"current": 100, "peak": 100}, {"current": 100, "peak": 16_777_317}),
    ],
)
def test_memory_delta_rejects_invalid_snapshots(
    reset: dict[str, int], measured: dict[str, int]
) -> None:
    report: dict[str, object] = {
        "linux_peak_reset": True,
        "baseline": {"current": 100, "peak": 100},
        "reset": reset,
        "measured": measured,
    }

    with pytest.raises(ValueError, match="memory"):
        memory_delta(report)


def test_pattern_cap_preserves_integer_numerators_and_floor() -> None:
    weights = {
        "evaluation": 13,
        "convert_byte": 17,
        "convert_value": 19,
        "node": 23,
        "pattern_compile": 29,
        "pattern_match": 31,
        "memory_base": 37,
        "memory_byte": 41,
    }

    result = pattern_cap(weights, [1])

    assert (
        result["time_numerator"]
        == 10_000_000_000_000 - 13 - 17 * 4096 - 19 * 256 - 3 * 23
    )
    assert result["time_denominator"] == 29 + 31 * 4096
    assert result["memory_numerator"] == 16_777_216 - 37
    assert result["memory_denominator"] == 41
    assert result["cap"] == (16_777_216 - 37) // 41 - 4096 - 6 * 4096


@pytest.mark.parametrize("denominator", [0, -1])
def test_pattern_cap_rejects_invalid_denominator(denominator: int) -> None:
    weights = {
        name: 1
        for name in (
            "evaluation",
            "convert_byte",
            "convert_value",
            "node",
            "pattern_compile",
            "pattern_match",
            "memory_base",
            "memory_byte",
        )
    }
    weights["memory_byte"] = denominator

    with pytest.raises(ValueError, match="positive"):
        pattern_cap(weights, [1])


def test_fit_requires_five_jobs() -> None:
    jobs: list[FitJob] = []

    with pytest.raises(ValueError, match="five"):
        fit_jobs(jobs)


def analytical_boundary(accepted: int) -> BoundaryReport:
    counts: list[BoundaryAttempt] = []
    lower, upper = 1, 1_048_576
    current = 1
    while True:
        status = "accepted" if current <= accepted else "refused"
        counts.append(
            BoundaryAttempt(
                count=current,
                status=status,
                source_sha256="arithmetic-fixture",
                result_sha256=None,
                error=None,
            )
        )
        if len(counts) == 1:
            current = upper
            continue
        if len(counts) == 2 and status == "accepted":
            break
        if status == "accepted":
            lower = current
        else:
            upper = current
        if upper - lower == 1:
            break
        current = (lower + upper) // 2
    return BoundaryReport(
        schema=1,
        form={},
        compiled_limit=10_485_760,
        upper_repetitions=1_048_576,
        attempts=counts,
        resources={},
        completion="arithmetic-fixture",
    )


def analytical_jobs() -> list[FitJob]:
    costs: dict[str, CostFamilyReport] = {}
    for family in dict.fromkeys(
        name for name, _ in OPERATION_IDENTITIES + REGEX_IDENTITIES
    ):
        cases: list[dict[str, object]] = []
        for case in cost_family_cases(family):
            residual = (
                case.size * case.size if family.startswith("quadratic_") else case.size
            )
            if family.startswith("regex_"):
                residual *= 100
            cases.append(
                {**observation(10 + residual, 10), "family": family, "size": case.size}
            )
        costs[family] = CostFamilyReport(
            schema=1,
            catalogue="arithmetic-fixture",
            family=family,
            warmup=3,
            samples=15,
            reference={},
            cases=cases,
            resources={},
            completion="arithmetic-fixture",
        )
    axes: dict[tuple[str, str], dict[str, object]] = {}
    for family in ("convert_bytes", "convert_values"):
        for mode in ("prepare", "native"):
            sizes = (
                (512, 1024, 2048, 4096)
                if family == "convert_bytes"
                else (16, 32, 64, 128)
            )
            axes[family, mode] = {
                "cases": [
                    {
                        **observation(10 + size, 10),
                        "case": f"{family}-{size}",
                        "predictor_units": size - 2,
                    }
                    for size in sizes
                ]
            }
    boundaries = {
        form.name: analytical_boundary(1_048_576 if form.anchor else 524_288)
        for form in PATTERN_FORMS
    }
    memory: dict[tuple[str, int], dict[str, object]] = {}
    table = {row: 30 for form in PATTERN_FORMS for row, _ in form.terms}
    for family, size in MEMORY_IDENTITIES + REGEX_MEMORY_IDENTITIES:
        case = memory_case(family, size)
        logical = (
            regex_logical_memory(family, size, table, 30, 30, 30)
            if family.startswith("regex_")
            else logical_memory(case, case.checked())
        )
        memory[family, size] = {
            "linux_peak_reset": True,
            "baseline": {"current": 1000, "peak": 1000},
            "reset": {"current": 1000, "peak": 1000},
            "measured": {"current": 1000, "peak": 1100 + 2 * logical},
        }
    return [
        FitJob(
            JobIdentity(
                "source",
                "tree",
                "run",
                "1",
                f"job-{index}",
                str(index),
                f"runner-{index}",
                "3.11.17",
                "0.13.1",
                "wheel",
                "manifest",
            ),
            costs,
            axes,
            boundaries,
            memory,
            {family: 100 for family in BOUND_FAMILIES},
        )
        for index in range(1, 6)
    ]


def test_complete_fit_retains_exact_evidence_and_proposes_constants() -> None:
    jobs = analytical_jobs()

    result = fit_jobs(jobs)

    assert result["reference_ns"] == {"numerator": 10, "denominator": 1}
    assert mapping(result["weights"])["pattern_compile"] == 5_000_000
    assert mapping(result["pattern"])["base"] == 30
    assert mapping(result["pattern"])["syntax"] == 30
    assert mapping(result["pattern"])["repetition"] == 30
    assert mapping(mapping(result["pattern"])["table"])["anchor"] == 30
    assert mapping(result["weights"])["memory_base"] == 150
    assert mapping(result["weights"])["memory_byte"] == 3
    assert integer(mapping(mapping(result["pattern"])["cap"])["cap"]) > 0
    assert len(sequence(result["series"])) > 5 * len(jobs[0].costs)
    assert json.loads(json.dumps(result)) == result


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entry", "1"),
        ("job_id", "job-1"),
        ("runner_name", "runner-1"),
        ("source_revision", "other"),
        ("python", "3.11.16"),
        ("uv", "0.13.0"),
    ],
)
def test_fit_rejects_invalid_job_identity(field: str, value: str) -> None:
    jobs = analytical_jobs()
    jobs[-1] = replace(jobs[-1], identity=replace(jobs[-1].identity, **{field: value}))

    with pytest.raises(ValueError, match="jobs|job|incomplete|selected"):
        validate_jobs(jobs)


@pytest.mark.parametrize(
    "field", ["costs", "axes", "boundaries", "memory", "conversion_minima"]
)
def test_fit_rejects_incomplete_manifest_maps(field: str) -> None:
    jobs = analytical_jobs()
    jobs[0] = replace(jobs[0], **{field: {}})

    with pytest.raises(ValueError, match="jobs|job|incomplete|selected"):
        validate_jobs(jobs)


def test_reference_median_uses_all_retained_bracket_streams() -> None:
    jobs = analytical_jobs()
    distinct_jobs: list[FitJob] = []
    for index, job in enumerate(jobs):
        report = {
            "cases": [
                {
                    "target": [bracket(100, 1)] * 3 + [bracket(100, 30 + index)] * 15,
                    "control_samples": [bracket(100, 1)] * 3
                    + [bracket(100, 20 + index)] * 15,
                }
            ]
        }
        distinct_jobs.append(
            replace(job, costs={}, axes={("convert_bytes", "native"): report})
        )

    reference, minima = frozen_reference(distinct_jobs)

    assert reference == 22
    assert minima == [20, 21, 22, 23, 24]


def test_boundary_envelopes_assign_wide_floor_to_any_only() -> None:
    jobs = analytical_jobs()
    changed = dict(jobs[0].boundaries)
    changed["range_wide"] = analytical_boundary(262_144)
    jobs[0] = replace(jobs[0], boundaries=changed)

    table, evidence = fit_pattern_table(jobs)

    assert table["any"] == 60
    assert table["range"] == 30
    assert table["literal"] == 30
    assert len(evidence) == 5 * len(PATTERN_FORMS)


def test_boundary_envelopes_reject_unbounded_nonanchor() -> None:
    jobs = analytical_jobs()
    changed = dict(jobs[0].boundaries)
    changed["literal_ascii"] = analytical_boundary(1_048_576)
    jobs[0] = replace(jobs[0], boundaries=changed)

    with pytest.raises(ValueError, match="censored"):
        fit_pattern_table(jobs)


def test_complexity_ratio_just_above_limit_is_not_rounded_down() -> None:
    minima = [
        Fraction(1),
        Fraction(5, 2) + Fraction(1, 10**20),
        Fraction(5),
        Fraction(10),
    ]
    residuals = [[value] * 18 for value in minima]

    with pytest.raises(ValueError, match="complexity"):
        fit_series([1, 2, 4, 8], [1, 2, 4, 8], residuals, quadratic=False)


def test_composed_boundary_uses_final_structural_envelope() -> None:
    jobs = analytical_jobs()
    changed = dict(jobs[0].boundaries)
    changed["nested_bracket"] = analytical_boundary(1000)
    jobs[0] = replace(jobs[0], boundaries=changed)
    table, evidence = fit_pattern_table(jobs)
    table["anchor"] = 30

    validate_pattern_envelopes(table, 30, 1_000_000, 30, evidence)

    point = next(
        item
        for item in evidence
        if item["form"] == "nested_bracket" and item["entry"] == "1"
    )
    assert point["atom_units"] == 60
    assert point["repeated_atom_units"] == 60_000
    assert point["envelope_satisfied"] is True


def test_composed_boundary_without_final_coverage_fails() -> None:
    jobs = analytical_jobs()
    changed = dict(jobs[0].boundaries)
    changed["nested_bracket"] = analytical_boundary(1000)
    jobs[0] = replace(jobs[0], boundaries=changed)
    table, evidence = fit_pattern_table(jobs)
    table["anchor"] = 30

    with pytest.raises(ValueError, match="final pattern envelope"):
        validate_pattern_envelopes(table, 30, 30, 30, evidence)


def test_conversion_normalizes_target_and_control_independently() -> None:
    preparation: dict[str, object] = {
        "target": [bracket(60, 30)] * 18,
        "control_samples": [bracket(10, 10)] * 18,
    }
    native: dict[str, object] = {
        "target": [bracket(60, 30)] * 18,
        "control_samples": [bracket(20, 10)] * 18,
    }

    result = combined_conversion(preparation, native, Fraction(10))

    assert result["prepare"] == [Fraction(10)] * 18
    assert result["native"] == [Fraction(0)] * 18
    assert result["combined"] == [Fraction(10)] * 18


def test_memory_fit_fails_without_identifiable_positive_maximum() -> None:
    jobs = analytical_jobs()
    reports = {
        key: {**value, "measured": {"current": 1000, "peak": 1100}}
        for key, value in jobs[0].memory.items()
    }
    jobs = [replace(job, memory=reports) for job in jobs]
    pattern: dict[str, object] = {
        "table": provisional_table(),
        "base": 1,
        "syntax": 1,
        "repetition": 32,
    }

    with pytest.raises(ValueError, match="not identifiable"):
        fit_memory(jobs, pattern)


@pytest.mark.parametrize("supported", [[], [1_048_577]])
def test_pattern_cap_requires_a_supported_pattern(supported: list[int]) -> None:
    weights = {
        name: 1
        for name in (
            "evaluation",
            "convert_byte",
            "convert_value",
            "node",
            "pattern_compile",
            "pattern_match",
            "memory_base",
            "memory_byte",
        )
    }

    with pytest.raises(ValueError, match="no supported pattern"):
        pattern_cap(weights, supported)


def test_pattern_cap_rejects_nonpositive_cap() -> None:
    weights = {
        name: 1
        for name in (
            "evaluation",
            "convert_byte",
            "convert_value",
            "node",
            "pattern_compile",
            "pattern_match",
            "memory_base",
            "memory_byte",
        )
    }
    weights["evaluation"] = 10_000_000_000_000

    with pytest.raises(ValueError, match="positive cap"):
        pattern_cap(weights, [1])


@pytest.mark.parametrize("field", ["job_id", "runner_name"])
def test_job_identity_rejects_empty_independent_identity(field: str) -> None:
    jobs = analytical_jobs()
    jobs[-1] = replace(jobs[-1], identity=replace(jobs[-1].identity, **{field: ""}))

    with pytest.raises(ValueError, match="identity"):
        validate_jobs(jobs)


def test_time_weight_rounds_up_after_headroom_once() -> None:
    sizes = [1, 2, 4, 8]
    residuals = [[Fraction(size, 7)] * 18 for size in sizes]

    result = fit_series(sizes, sizes, residuals, quadratic=False)

    assert result["slope_ns"] == Fraction(1, 7)
    assert result["headroom_slope_ns"] == Fraction(3, 14)
    assert result["weight_fs"] == 214_286


def test_selected_conversion_minimum_over_stop_fails_complete_fit() -> None:
    jobs = analytical_jobs()
    minima = {**jobs[0].conversion_minima, "ascii": 2_500_001}
    jobs[0] = replace(jobs[0], conversion_minima=minima)

    with pytest.raises(ValueError, match="selected conversion"):
        fit_jobs(jobs)
