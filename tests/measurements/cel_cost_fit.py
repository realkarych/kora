import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import TypedDict, cast

from tests.measurements.cel_bound_workload import BOUND_FAMILIES
from tests.measurements.cel_cost_calibration import (
    CostFamilyReport,
    complexity_of,
    fit_memory_weights,
    mapping,
    normalized_sample,
    sequence,
    upper_slope,
)
from tests.measurements.cel_cost_catalogue import (
    AXIS_FAMILIES,
    MEMORY_IDENTITIES,
    OPERATION_IDENTITIES,
    cost_family_cases,
    memory_case,
)
from tests.measurements.cel_cost_workload import SAMPLES, WARMUP, logical_memory
from tests.measurements.cel_regex_workload import (
    MATCH_PATTERNS,
    PATTERN_FORMS,
    REGEX_IDENTITIES,
    REGEX_MEMORY_IDENTITIES,
    REPETITIONS,
    BoundaryReport,
    boundary_limits,
    form_by_name,
)


@dataclass(frozen=True)
class JobIdentity:
    source_revision: str
    tree: str
    run_id: str
    run_attempt: str
    job_id: str
    entry: str
    runner_name: str
    python: str
    uv: str
    wheel_sha256: str
    manifest_sha256: str


@dataclass(frozen=True)
class FitJob:
    identity: JobIdentity
    costs: dict[str, CostFamilyReport]
    axes: dict[tuple[str, str], dict[str, object]]
    boundaries: dict[str, BoundaryReport]
    memory: dict[tuple[str, int], dict[str, object]]
    conversion_minima: dict[str, int]


class FitComplexity(TypedDict):
    exponent: float
    doubling_ratios: list[Fraction]
    within_limits: bool


class SeriesFit(TypedDict):
    sizes: list[int]
    units: list[int]
    residuals_ns: list[list[Fraction]]
    minima_ns: list[Fraction]
    slope_ns: Fraction
    headroom_slope_ns: Fraction
    weight_fs: int
    complexity: FitComplexity


class ConversionResiduals(TypedDict):
    prepare: list[Fraction]
    native: list[Fraction]
    combined: list[Fraction]
    native_batch_differences: list[int]


def integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("measurement must be an exact integer")
    return value


def samples(observation: Mapping[str, object], name: str) -> list[dict[str, object]]:
    result = [mapping(item) for item in sequence(observation[name])]
    if len(result) != WARMUP + SAMPLES:
        raise ValueError("incomplete paired calibration samples")
    return result


def normalize(sample: Mapping[str, object], reference: Fraction) -> Fraction:
    return normalized_sample(
        integer(sample["elapsed_ns"]),
        integer(sample["calls"]),
        integer(sample["reference_before_ns"]),
        integer(sample["reference_after_ns"]),
        integer(sample["reference_calls"]),
        reference,
    )


def residual_vector(
    observation: Mapping[str, object], reference: Fraction
) -> list[Fraction]:
    return [
        normalize(target, reference) - normalize(control, reference)
        for target, control in zip(
            samples(observation, "target"),
            samples(observation, "control_samples"),
            strict=True,
        )
    ]


def require_positive_retained(values: Sequence[Fraction]) -> None:
    if len(values) != WARMUP + SAMPLES:
        raise ValueError("incomplete paired calibration samples")
    if any(value <= 0 for value in values[WARMUP:]):
        raise ValueError("retained residual is not identifiable")


def combined_conversion(
    preparation: Mapping[str, object],
    native: Mapping[str, object],
    reference: Fraction,
) -> ConversionResiduals:
    differences = [
        integer(target["elapsed_ns"]) - integer(control["elapsed_ns"])
        for target, control in zip(
            samples(native, "target"), samples(native, "control_samples"), strict=True
        )
    ]
    if any(value <= 0 for value in differences[WARMUP:]):
        raise ValueError("raw native conversion contribution is not identifiable")
    prepared = residual_vector(preparation, reference)
    converted = residual_vector(native, reference)
    combined = [a + b for a, b in zip(prepared, converted, strict=True)]
    require_positive_retained(combined)
    return ConversionResiduals(
        prepare=prepared,
        native=converted,
        combined=combined,
        native_batch_differences=differences,
    )


def fit_series(
    sizes: Sequence[int],
    units: Sequence[int],
    residuals: Sequence[Sequence[Fraction]],
    *,
    quadratic: bool,
) -> SeriesFit:
    for vector in residuals:
        require_positive_retained(vector)
    minima = [min(vector[WARMUP:]) for vector in residuals]
    slope = upper_slope(list(zip(units, minima, strict=True)))
    regression = complexity_of(
        list(zip(sizes, minima, strict=True)), quadratic=quadratic
    )
    ratios = [
        after / before for before, after in zip(minima[:-1], minima[1:], strict=True)
    ]
    complexity = FitComplexity(
        exponent=regression["exponent"],
        doubling_ratios=ratios,
        within_limits=regression["exponent"] <= (2.25 if quadratic else 1.25)
        and all(ratio <= (5 if quadratic else Fraction(5, 2)) for ratio in ratios),
    )
    if not complexity["within_limits"]:
        raise ValueError("calibration series exceeds complexity limits")
    headroom = slope * Fraction(3, 2)
    return SeriesFit(
        sizes=list(sizes),
        units=list(units),
        residuals_ns=[list(vector) for vector in residuals],
        minima_ns=minima,
        slope_ns=slope,
        headroom_slope_ns=headroom,
        weight_fs=math.ceil(headroom * 1_000_000),
        complexity=complexity,
    )


def validate_jobs(jobs: Sequence[FitJob]) -> None:
    if len(jobs) != 5:
        raise ValueError("fitting requires exactly five independent jobs")
    identities = [job.identity for job in jobs]
    if any(not identity.job_id or not identity.runner_name for identity in identities):
        raise ValueError("job and runner identity must be nonempty")
    if {identity.entry for identity in identities} != {str(i) for i in range(1, 6)}:
        raise ValueError("jobs must cover exactly entries 1 through 5")
    if (
        len({identity.job_id for identity in identities}) != 5
        or len({identity.runner_name for identity in identities}) != 5
    ):
        raise ValueError("jobs and runners must be independent")
    shared = {
        (
            identity.source_revision,
            identity.tree,
            identity.run_id,
            identity.run_attempt,
            identity.python,
            identity.uv,
            identity.wheel_sha256,
            identity.manifest_sha256,
        )
        for identity in identities
    }
    if len(shared) != 1 or any(not part for part in next(iter(shared))):
        raise ValueError("job source, environment or manifest identities differ")
    if identities[0].python != "3.11.17" or identities[0].uv != "0.13.1":
        raise ValueError("job toolchain is not the reference toolchain")
    families = {family for family, _ in OPERATION_IDENTITIES + REGEX_IDENTITIES}
    for job in jobs:
        if set(job.costs) != families:
            raise ValueError("incomplete cost family map")
        for family, report in job.costs.items():
            expected = [(case.family, case.size) for case in cost_family_cases(family)]
            actual = [(item["family"], item["size"]) for item in report["cases"]]
            if actual != expected:
                raise ValueError("incomplete cost case map")
        if set(job.axes) != {
            (family, mode) for family in AXIS_FAMILIES for mode in ("prepare", "native")
        }:
            raise ValueError("incomplete conversion axis map")
        for (family, _), report in job.axes.items():
            sizes = (
                (512, 1024, 2048, 4096)
                if family == "convert_bytes"
                else (16, 32, 64, 128)
            )
            cases = [mapping(case) for case in sequence(report["cases"])]
            if [case["case"] for case in cases] != [
                f"{family}-{size}" for size in sizes
            ]:
                raise ValueError("incomplete conversion axis case map")
            if [case["predictor_units"] for case in cases] != [
                size - 2 for size in sizes
            ]:
                raise ValueError("conversion axis predictor changed")
        if set(job.boundaries) != {form.name for form in PATTERN_FORMS}:
            raise ValueError("incomplete regex boundary map")
        if set(job.memory) != set(MEMORY_IDENTITIES + REGEX_MEMORY_IDENTITIES):
            raise ValueError("incomplete memory case map")
        if set(job.conversion_minima) != set(BOUND_FAMILIES) or any(
            type(value) is not int or not 0 <= value <= 2_500_000
            for value in job.conversion_minima.values()
        ):
            raise ValueError("selected conversion minima do not qualify")


def frozen_reference(jobs: Sequence[FitJob]) -> tuple[Fraction, list[Fraction]]:
    if len(jobs) != 5:
        raise ValueError("reference requires five jobs")
    minima: list[Fraction] = []
    for job in jobs:
        observations = [
            case for report in job.costs.values() for case in report["cases"]
        ]
        observations.extend(
            mapping(case)
            for report in job.axes.values()
            for case in sequence(report["cases"])
        )
        references = [
            Fraction(integer(sample[field]), integer(sample["reference_calls"]))
            for observation in observations
            for stream in ("target", "control_samples")
            for sample in samples(observation, stream)[WARMUP:]
            for field in ("reference_before_ns", "reference_after_ns")
        ]
        if not references or any(value <= 0 for value in references):
            raise ValueError("reference observations must be positive and complete")
        minima.append(min(references))
    return sorted(minima)[2], minima


def memory_delta(report: Mapping[str, object]) -> int:
    if report["linux_peak_reset"] is not True:
        raise ValueError("memory measurement requires Linux peak reset")
    readings = [mapping(report[key]) for key in ("baseline", "reset", "measured")]
    for reading in readings:
        if set(reading) != {"current", "peak"}:
            raise ValueError("invalid memory snapshot")
        current, peak = integer(reading["current"]), integer(reading["peak"])
        if current <= 0 or peak < current:
            raise ValueError("invalid memory snapshot")
    reset, measured = readings[1:]
    if integer(measured["peak"]) < integer(reset["peak"]):
        raise ValueError("memory peak decreased after reset")
    delta = integer(measured["peak"]) - integer(reset["current"])
    if not 0 <= delta <= 16_777_216:
        raise ValueError("observed memory exceeds budget")
    return delta


def pattern_parts(
    family: str, size: int, table: Mapping[str, int], repetition: int
) -> tuple[str, int]:
    if (family, size) not in REGEX_IDENTITIES:
        raise ValueError("undeclared regex model case")
    if family == "regex_base":
        return "", 0
    if family.startswith(("regex_atoms_", "regex_anchor_")):
        form = form_by_name(
            family.removeprefix("regex_atoms_").removeprefix("regex_anchor_")
        )
        return form.pattern(size), form.unit(table) * size
    if family.startswith("regex_structure_"):
        name = family.removeprefix("regex_structure_")
        pattern = {
            "captures": "()" * size,
            "noncaptures": "(?:)" * size,
            "alternatives": "|" * size,
            "flags": "(?i)" * size,
            "nested": "(" * size + ")" * size,
        }[name]
        return pattern, 0
    if family.startswith("regex_repetition_"):
        name = family.removeprefix("regex_repetition_")
        _, operator, factor = next(item for item in REPETITIONS if item[0] == name)
        return ("a" + operator) * size + "b", (
            table["literal"] * factor + repetition
        ) * size + table["literal"]
    name = (
        family.removeprefix("regex_match_")
        .removesuffix("_success")
        .removesuffix("_late_failure")
    )
    _, pattern, _ = next(item for item in MATCH_PATTERNS if item[0] == name)
    atoms = {
        "ascii": table["range"],
        "class": table["class:w"],
        "unicode": table["unicode:L"],
        "wide": max(table["range"], table["any"]),
        "hostile": 3 * table["literal"],
        "mixed": table["unicode:L"] + table["class:d"],
    }
    return pattern, 2 * table["anchor"] + 2 * atoms[name] + repetition


def pattern_units(
    family: str,
    size: int,
    table: Mapping[str, int],
    base: int,
    syntax: int,
    repetition: int,
) -> int:
    if any(
        type(value) is not int or value <= 0
        for value in (*table.values(), base, syntax, repetition)
    ):
        raise ValueError("pattern constants must be positive integers")
    pattern, atoms = pattern_parts(family, size, table, repetition)
    return base + max(atoms, syntax * len(pattern))


def regex_logical_memory(
    family: str,
    size: int,
    table: Mapping[str, int],
    base: int,
    syntax: int,
    repetition: int,
) -> int:
    if (family, size) not in REGEX_MEMORY_IDENTITIES:
        raise ValueError("undeclared regex memory model case")
    pattern, _ = pattern_parts(family, size, table, repetition)
    units = pattern_units(family, size, table, base, syntax, repetition)
    pattern_bytes = len(json.dumps(pattern, ensure_ascii=False).encode("utf-8"))
    if family.startswith("regex_match_"):
        converted = size + 2
        left_list = 8 * 24 + 9
        right_list = 8 * (4 if family.endswith("_success") else 5) + 9
        return converted + max(pattern_bytes + units, left_list + right_list)
    subject_bytes = 4 if family.startswith("regex_anchor_") else 2
    return subject_bytes + pattern_bytes + units


def pattern_cap(
    weights: Mapping[str, int], supported_units: Sequence[int]
) -> dict[str, int]:
    names = (
        "evaluation",
        "convert_byte",
        "convert_value",
        "node",
        "pattern_compile",
        "pattern_match",
        "memory_byte",
    )
    if any(type(weights[name]) is not int or weights[name] <= 0 for name in names):
        raise ValueError("cap coefficients must be positive integers")
    if type(weights["memory_base"]) is not int or weights["memory_base"] < 0:
        raise ValueError("memory base must be nonnegative")
    time_numerator = (
        10_000_000_000_000
        - weights["evaluation"]
        - weights["convert_byte"] * 4096
        - weights["convert_value"] * 256
        - 3 * weights["node"]
    )
    time_denominator = weights["pattern_compile"] + weights["pattern_match"] * 4096
    memory_numerator = 16_777_216 - weights["memory_base"]
    memory_denominator = weights["memory_byte"]
    time_cap = time_numerator // time_denominator
    memory_cap = memory_numerator // memory_denominator - 4096 - 6 * 4096
    cap = min(1_048_576, time_cap, memory_cap)
    if cap <= 0 or not any(0 < units <= cap for units in supported_units):
        raise ValueError("no supported pattern fits the positive cap")
    return {
        "time_numerator": time_numerator,
        "time_denominator": time_denominator,
        "time_cap": time_cap,
        "memory_numerator": memory_numerator,
        "memory_denominator": memory_denominator,
        "memory_cap": memory_cap,
        "cap": cap,
    }


def fit_pattern_table(
    jobs: Sequence[FitJob],
) -> tuple[dict[str, int], list[dict[str, object]]]:
    if len(jobs) != 5:
        raise ValueError("pattern table requires five jobs")
    table: dict[str, int] = {}
    evidence: list[dict[str, object]] = []
    for job in jobs:
        for form in PATTERN_FORMS:
            report = job.boundaries[form.name]
            if (
                report["compiled_limit"] != 10_485_760
                or report["upper_repetitions"] != 1_048_576
            ):
                raise ValueError("regex boundary limits changed")
            accepted, refused = boundary_limits(report["attempts"])
            quotient = None if refused is None else Fraction(10_485_760, accepted)
            fitted = None if quotient is None else math.ceil(quotient * Fraction(3, 2))
            row: str | None = None
            if not form.anchor:
                if fitted is None:
                    raise ValueError("censored nonanchor boundary is not identifiable")
                if len(form.terms) == 1 and form.terms[0][1] == 1:
                    row = form.terms[0][0]
                    if form.minimum_any and row in (
                        "literal",
                        "caseless_literal",
                        "range",
                        "caseless_range",
                    ):
                        row = "any"
                    table[row] = max(table.get(row, 0), fitted)
            evidence.append(
                {
                    "entry": job.identity.entry,
                    "form": form.name,
                    "accepted_count": accepted,
                    "refused_count": refused,
                    "quotient": quotient,
                    "headroom_quotient": None
                    if quotient is None
                    else quotient * Fraction(3, 2),
                    "rounded_unit": fitted,
                    "assigned_row": row,
                    "censored": refused is None,
                }
            )
    expected = {
        row for form in PATTERN_FORMS if not form.anchor for row, _ in form.terms
    }
    if set(table) != expected or any(value <= 0 for value in table.values()):
        raise ValueError("pattern table rows are incomplete")
    return table, evidence


def validate_pattern_envelopes(
    table: Mapping[str, int],
    base: int,
    syntax: int,
    repetition: int,
    boundaries: list[dict[str, object]],
) -> None:
    for point in boundaries:
        form = form_by_name(str(point["form"]))
        count = integer(point["accepted_count"])
        atoms = form.unit(table)
        repeated_atoms = atoms * count
        text = form.pattern(count, repetition=True)
        complete = base + max(repeated_atoms + repetition, syntax * len(text))
        required = None if point["censored"] else 15_728_640
        point.update(
            {
                "atom_units": atoms,
                "repeated_atom_units": repeated_atoms,
                "repetition_overhead": repetition,
                "syntax_units": syntax * len(text),
                "base_units": base,
                "complete_pattern_units": complete,
                "required_envelope": required,
                "envelope_satisfied": None
                if required is None
                else complete >= required,
            }
        )
        if form.anchor:
            point["structural_unit"] = table["anchor"]
        if required is not None and complete < required:
            raise ValueError("measured boundary exceeds the final pattern envelope")


def family_residuals(
    job: FitJob, family: str, reference: Fraction
) -> list[list[Fraction]]:
    return [residual_vector(case, reference) for case in job.costs[family]["cases"]]


def record_series(
    evidence: list[dict[str, object]],
    job: FitJob,
    family: str,
    coefficient: str,
    fit: SeriesFit,
) -> None:
    evidence.append(
        {
            "entry": job.identity.entry,
            "family": family,
            "coefficient": coefficient,
            **fit,
        }
    )


def fit_operations(
    jobs: Sequence[FitJob], reference: Fraction, evidence: list[dict[str, object]]
) -> dict[str, int]:
    weights: dict[str, int] = {}
    bases: list[Fraction] = []
    for job in jobs:
        case = job.costs["evaluation"]["cases"][0]
        streams = {
            stream: [normalize(sample, reference) for sample in samples(case, stream)]
            for stream in ("target", "control_samples")
        }
        minimum = min(value for values in streams.values() for value in values[WARMUP:])
        bases.append(minimum)
        evidence.append(
            {
                "entry": job.identity.entry,
                "family": "evaluation",
                "coefficient": "evaluation",
                "normalized_ns": streams,
                "minimum_ns": minimum,
            }
        )
        for family in dict.fromkeys(
            name for name, _ in OPERATION_IDENTITIES if name != "evaluation"
        ):
            cases = cost_family_cases(family)
            fit = fit_series(
                [case.size for case in cases],
                [case.units for case in cases],
                family_residuals(job, family, reference),
                quadratic=family.startswith("quadratic_"),
            )
            coefficient = cases[0].coefficient
            weights[coefficient] = max(weights.get(coefficient, 0), fit["weight_fs"])
            record_series(evidence, job, family, coefficient, fit)
        for family in AXIS_FAMILIES:
            phases = {
                mode: [
                    mapping(case) for case in sequence(job.axes[family, mode]["cases"])
                ]
                for mode in ("prepare", "native")
            }
            combined = [
                combined_conversion(prepare, native, reference)
                for prepare, native in zip(
                    phases["prepare"], phases["native"], strict=True
                )
            ]
            sizes = (
                [512, 1024, 2048, 4096]
                if family == "convert_bytes"
                else [16, 32, 64, 128]
            )
            fit = fit_series(
                sizes,
                [size - 2 for size in sizes],
                [item["combined"] for item in combined],
                quadratic=False,
            )
            coefficient = (
                "convert_byte" if family == "convert_bytes" else "convert_value"
            )
            weights[coefficient] = max(weights.get(coefficient, 0), fit["weight_fs"])
            record_series(evidence, job, family, coefficient, fit)
            evidence[-1]["phase_residuals"] = combined
    weights["evaluation"] = math.ceil(max(bases) * Fraction(3, 2) * 1_000_000)
    return weights


def structural_unit(
    fits: Sequence[SeriesFit], compile_weight: int
) -> tuple[int, Fraction]:
    if not fits or compile_weight <= 0:
        raise ValueError(
            "positive identifiable structural slope and compile divisor required"
        )
    exact = max(fit["headroom_slope_ns"] for fit in fits) * 1_000_000 / compile_weight
    if exact <= 0:
        raise ValueError("structural unit is not identifiable")
    return math.ceil(exact), exact


def fit_patterns(
    jobs: Sequence[FitJob],
    reference: Fraction,
    evidence: list[dict[str, object]],
) -> tuple[dict[str, int], dict[str, object]]:
    table, boundaries = fit_pattern_table(jobs)
    compile_fits: list[SeriesFit] = []
    for job in jobs:
        for form in PATTERN_FORMS:
            if form.anchor:
                continue
            family = "regex_atoms_" + form.name
            cases = cost_family_cases(family)
            fit = fit_series(
                [case.size for case in cases],
                [form.unit(table) * case.size for case in cases],
                family_residuals(job, family, reference),
                quadratic=False,
            )
            compile_fits.append(fit)
            record_series(evidence, job, family, "pattern_compile", fit)
    compile_weight = max(fit["weight_fs"] for fit in compile_fits)
    base_minima: list[Fraction] = []
    structures: dict[str, list[SeriesFit]] = {
        "syntax": [],
        "anchor": [],
        "repetition": [],
    }
    prefixes = {
        "syntax": "regex_structure_",
        "anchor": "regex_anchor_",
        "repetition": "regex_repetition_",
    }
    for job in jobs:
        base_vector = family_residuals(job, "regex_base", reference)[0]
        require_positive_retained(base_vector)
        minimum = min(base_vector[WARMUP:])
        base_minima.append(minimum)
        evidence.append(
            {
                "entry": job.identity.entry,
                "family": "regex_base",
                "coefficient": "pattern_base",
                "residuals_ns": base_vector,
                "minimum_ns": minimum,
            }
        )
        for name, prefix in prefixes.items():
            for family in dict.fromkeys(
                family for family, _ in REGEX_IDENTITIES if family.startswith(prefix)
            ):
                cases = cost_family_cases(family)
                fit = fit_series(
                    [case.size for case in cases],
                    [case.units for case in cases],
                    family_residuals(job, family, reference),
                    quadratic=False,
                )
                structures[name].append(fit)
                record_series(evidence, job, family, "pattern_" + name, fit)
    base_exact = max(base_minima) * Fraction(3, 2) * 1_000_000 / compile_weight
    base = math.ceil(base_exact)
    syntax, syntax_exact = structural_unit(structures["syntax"], compile_weight)
    repetition, repetition_exact = structural_unit(
        structures["repetition"], compile_weight
    )
    anchor, anchor_exact = structural_unit(structures["anchor"], compile_weight)
    table["anchor"] = anchor
    validate_pattern_envelopes(table, base, syntax, repetition, boundaries)
    match_weights: list[int] = []
    for job in jobs:
        for family in dict.fromkeys(
            family
            for family, _ in REGEX_IDENTITIES
            if family.startswith("regex_match_")
        ):
            cases = cost_family_cases(family)
            fit = fit_series(
                [case.size for case in cases],
                [
                    case.units
                    * pattern_units(family, case.size, table, base, syntax, repetition)
                    for case in cases
                ],
                family_residuals(job, family, reference),
                quadratic=False,
            )
            match_weights.append(fit["weight_fs"])
            record_series(evidence, job, family, "pattern_match", fit)
    return {"pattern_compile": compile_weight, "pattern_match": max(match_weights)}, {
        "table": table,
        "base": base,
        "syntax": syntax,
        "repetition": repetition,
        "base_exact": base_exact,
        "syntax_exact": syntax_exact,
        "repetition_exact": repetition_exact,
        "anchor_exact": anchor_exact,
        "boundaries": boundaries,
    }


def pattern_parameters(
    pattern: Mapping[str, object],
) -> tuple[dict[str, int], int, int, int]:
    table = {key: integer(value) for key, value in mapping(pattern["table"]).items()}
    return (
        table,
        integer(pattern["base"]),
        integer(pattern["syntax"]),
        integer(pattern["repetition"]),
    )


def fit_memory(
    jobs: Sequence[FitJob], pattern: Mapping[str, object]
) -> tuple[dict[str, int], list[dict[str, object]]]:
    table, base, syntax, repetition = pattern_parameters(pattern)
    denominators: dict[tuple[str, int], int] = {}
    for family, size in MEMORY_IDENTITIES + REGEX_MEMORY_IDENTITIES:
        if family.startswith("regex_"):
            logical = regex_logical_memory(
                family, size, table, base, syntax, repetition
            )
        else:
            case = memory_case(family, size)
            logical = logical_memory(case, case.checked())
        denominators[family, size] = logical
    controls: list[int] = []
    positive: list[tuple[int, int]] = []
    points: list[dict[str, object]] = []
    for job in jobs:
        for (family, size), report in job.memory.items():
            logical = denominators[family, size]
            delta = memory_delta(report)
            if logical == 0:
                controls.append(delta)
            else:
                positive.append((logical, delta))
            points.append(
                {
                    "entry": job.identity.entry,
                    "family": family,
                    "size": size,
                    "logical_bytes": logical,
                    "observed_delta": delta,
                    "zero_denominator": logical == 0,
                }
            )
    fixed, multiplier = fit_memory_weights(controls, positive)
    for point in points:
        logical = integer(point["logical_bytes"])
        observed = integer(point["observed_delta"])
        charge = fixed + logical * multiplier
        if observed > charge or observed > 16_777_216:
            raise ValueError("observed memory is outside the proposed envelope")
        point.update(
            {
                "fixed_allowance": fixed,
                "ratio_after_fixed": None
                if logical == 0
                else Fraction(observed - fixed, logical),
                "headroom_ratio": None
                if logical == 0
                else Fraction(3 * (observed - fixed), 2 * logical),
                "proposed_memory_charge": charge,
                "observed_within_estimate": True,
                "observed_within_budget": True,
                "memory_refused": charge > 16_777_216,
            }
        )
    return {"memory_base": fixed, "memory_byte": multiplier}, points


def exact_report(value: object) -> object:
    if isinstance(value, Fraction):
        return {"numerator": value.numerator, "denominator": value.denominator}
    if value is None or type(value) in (str, int, bool, float):
        return value
    if isinstance(value, dict):
        return {
            key: exact_report(item)
            for key, item in mapping(cast(object, value)).items()
        }
    if isinstance(value, (list, tuple)):
        return [exact_report(item) for item in cast(Sequence[object], value)]
    raise ValueError("unserializable fit evidence")


def fit_jobs(jobs: Sequence[FitJob]) -> dict[str, object]:
    validate_jobs(jobs)
    ordered = sorted(jobs, key=lambda job: job.identity.entry)
    reference, reference_minima = frozen_reference(ordered)
    evidence: list[dict[str, object]] = []
    weights = fit_operations(ordered, reference, evidence)
    regex_weights, pattern = fit_patterns(ordered, reference, evidence)
    weights.update(regex_weights)
    memory_weights, memory_points = fit_memory(ordered, pattern)
    weights.update(memory_weights)
    table, base, syntax, repetition = pattern_parameters(pattern)
    units = {
        (family, size): pattern_units(family, size, table, base, syntax, repetition)
        for family, size in REGEX_IDENTITIES
    }
    cap = pattern_cap(weights, list(units.values()))
    pattern["cap"] = cap
    pattern["case_units"] = [
        {
            "family": family,
            "size": size,
            "units": value,
            "pattern_cap_refused": value > cap["cap"],
        }
        for (family, size), value in units.items()
    ]
    for point in memory_points:
        family = str(point["family"])
        size = integer(point["size"])
        point["pattern_cap_refused"] = (
            family.startswith("regex_") and units[family, size] > cap["cap"]
        )
        point["admission_status"] = (
            "refused_by_memory_or_pattern_cap"
            if point["memory_refused"] or point["pattern_cap_refused"]
            else "time_admission_not_evaluated"
        )
    return mapping(
        exact_report(
            {
                "schema": 1,
                "status": "proposed_fit",
                "reference_ns": reference,
                "job_reference_minima_ns": reference_minima,
                "entries": [job.identity.entry for job in ordered],
                "weights": weights,
                "pattern": pattern,
                "series": evidence,
                "memory_points": memory_points,
                "headroom": Fraction(3, 2),
                "authenticity": "requires_collector_qualification",
            }
        )
    )
