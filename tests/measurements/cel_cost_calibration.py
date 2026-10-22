import argparse
import hashlib
import importlib.metadata
import itertools
import json
import math
import os
import platform
import shutil
import sys
import sysconfig
import time
import tomllib
from collections.abc import Sequence
from fractions import Fraction
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import unquote, urlsplit

from kora.cel_eval.convert import BoundValues, bind
from tests.cel_eval.children import full_path
from tests.measurements.cel_bound_workload import (
    BOUND_FAMILIES,
    BOUND_PAIRS,
    bound_case,
    conversion_samples,
    select_bounds,
)
from tests.measurements.cel_cost_catalogue import (
    OPERATION_IDENTITIES,
    memory_case,
    operation_case,
    reference_case,
)
from tests.measurements.cel_cost_workload import (
    FULL_TIMINGS,
    NATIVE_TIMINGS,
    SAMPLES,
    WARMUP,
    ConversionCase,
    binding_statistics,
    digest,
    logical_memory,
    pilot_cases,
)
from tests.measurements.cel_measurement_process import ProcessResult, run_process
from tests.measurements.harness import Sample, new_report

ROOT: Path = Path(__file__).resolve().parents[2]
STOP_NS: int = 2_500_000
PILOT_SECONDS: float = 240.0
PRELUDE: str = (
    "import json, sys; sys.path[:] = json.loads(sys.argv[1]); "
    "from tests.measurements.cel_cost_workload import run_case; "
    "run_case(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else 'pilot')"
)
COST_PRELUDE: str = (
    "import json, sys; sys.path[:] = json.loads(sys.argv[1]); "
    "from tests.measurements.cel_cost_workload import run_cost_family; "
    "run_cost_family(sys.argv[2])"
)
MEMORY_PRELUDE: str = (
    "import json, sys; sys.path[:] = json.loads(sys.argv[1]); "
    "from tests.measurements.cel_cost_workload import run_memory_case; "
    "run_memory_case(sys.argv[2], int(sys.argv[3]))"
)


class CostFamilyReport(TypedDict):
    schema: int
    catalogue: str
    family: str
    warmup: int
    samples: int
    reference: dict[str, object]
    cases: list[dict[str, object]]
    resources: dict[str, object]
    completion: str


class Complexity(TypedDict):
    exponent: float
    doubling_ratios: list[float]
    within_limits: bool


def positive_integer(value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("measurement count must be a positive integer")


def normalized_sample(
    elapsed: int,
    calls: int,
    reference_before: int,
    reference_after: int,
    reference_calls: int,
    frozen_reference: Fraction,
) -> Fraction:
    for value in (elapsed, calls, reference_before, reference_after, reference_calls):
        positive_integer(value)
    if frozen_reference <= 0:
        raise ValueError("frozen reference must be positive")
    mean_reference = Fraction(reference_before + reference_after, 2 * reference_calls)
    return Fraction(elapsed, calls) * frozen_reference / mean_reference


def paired_residuals(
    targets: Sequence[dict[str, object]],
    controls: Sequence[dict[str, object]],
    frozen_reference: Fraction,
) -> list[Fraction]:
    if len(targets) != WARMUP + SAMPLES or len(controls) != WARMUP + SAMPLES:
        raise ValueError("incomplete paired calibration samples")
    residuals: list[Fraction] = []
    for target, control in zip(targets, controls, strict=True):
        normalized: list[Fraction] = []
        for sample in (target, control):
            normalized.append(
                normalized_sample(
                    cast(int, sample["elapsed_ns"]),
                    cast(int, sample["calls"]),
                    cast(int, sample["reference_before_ns"]),
                    cast(int, sample["reference_after_ns"]),
                    cast(int, sample["reference_calls"]),
                    frozen_reference,
                )
            )
        residuals.append(normalized[0] - normalized[1])
    if any(value <= 0 for value in residuals[WARMUP:]):
        raise ValueError("paired normalized residual is not identifiable")
    return residuals


def upper_slope(points: Sequence[tuple[int, Fraction]]) -> Fraction:
    sizes = [size for size, _ in points]
    if len(sizes) != 4 or len(set(sizes)) != 4 or sizes != sorted(sizes):
        raise ValueError("a calibration series requires four distinct ordered sizes")
    for size, residual in points:
        positive_integer(size)
        if residual <= 0:
            raise ValueError("a calibration residual is not identifiable")
    return max(residual / size for size, residual in points)


def fit_time_weight(series: Sequence[Sequence[tuple[int, Fraction]]]) -> int:
    if not series:
        raise ValueError("no calibration series")
    slope = max(upper_slope(points) for points in series)
    return math.ceil(slope * Fraction(3, 2) * 1_000_000)


def fit_memory_weights(
    controls: Sequence[int], positive_models: Sequence[tuple[int, int]]
) -> tuple[int, int]:
    if not controls or not positive_models:
        raise ValueError("memory calibration requires fixed and variable cases")
    if any(type(value) is not int or value < 0 for value in controls):
        raise ValueError("invalid fixed memory observation")
    fixed = math.ceil(Fraction(3, 2) * max(controls))
    ratios: list[Fraction] = []
    for logical, observed in positive_models:
        if type(logical) is not int or logical <= 0:
            raise ValueError("memory calibration requires a positive logical size")
        if type(observed) is not int or observed < 0:
            raise ValueError("invalid variable memory observation")
        ratios.append(Fraction(observed - fixed, logical))
    slope = max(ratios)
    if slope <= 0:
        raise ValueError("variable memory contribution is not identifiable")
    return fixed, max(1, math.ceil(Fraction(3, 2) * slope))


def fit_pattern_unit(accepted_counts: Sequence[int]) -> int:
    if len(accepted_counts) < 5:
        raise ValueError("pattern calibration requires five independent jobs")
    for count in accepted_counts:
        positive_integer(count)
    unit = max(Fraction(10_485_760, count) for count in accepted_counts)
    return math.ceil(Fraction(3, 2) * unit)


def complexity_of(
    points: Sequence[tuple[int, Fraction]], *, quadratic: bool
) -> Complexity:
    upper_slope(points)
    if any(
        after != before * 2
        for (before, _), (after, _) in zip(points[:-1], points[1:], strict=True)
    ):
        raise ValueError("complexity sizes must double")
    x = [math.log2(size) for size, _ in points]
    y = [math.log2(value) for _, value in points]
    mean_x = sum(x) / 4
    mean_y = sum(y) / 4
    exponent = sum(
        (size - mean_x) * (elapsed - mean_y) for size, elapsed in zip(x, y, strict=True)
    ) / sum((size - mean_x) ** 2 for size in x)
    ratios = [
        float(after / before)
        for (_, before), (_, after) in zip(points[:-1], points[1:], strict=True)
    ]
    return Complexity(
        exponent=exponent,
        doubling_ratios=ratios,
        within_limits=exponent <= (2.25 if quadratic else 1.25)
        and all(ratio <= (5.0 if quadratic else 2.5) for ratio in ratios),
    )


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected a measurement object")
    entries = cast(dict[object, object], value)
    if not all(type(key) is str for key in entries):
        raise ValueError("measurement keys must be strings")
    return cast(dict[str, object], entries)


def sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("expected a measurement sequence")
    return cast(list[object], value)


def timings_of(observation: dict[str, object]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for name, raw in mapping(observation["timings"]).items():
        values = sequence(raw)
        if len(values) != WARMUP + SAMPLES or any(
            type(value) is not int or value < 0 for value in values
        ):
            raise ValueError("invalid raw timing vector")
        result[name] = cast(list[int], values)
    return result


def unique_mapping(entries: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in entries:
        if key in result:
            raise ValueError("duplicate measurement field")
        result[key] = value
    return result


def reject_constant(value: str) -> object:
    raise ValueError("nonfinite measurement value")


def validate_cost_family(content: bytes, family: str) -> CostFamilyReport:
    raw: object = json.loads(
        content, object_pairs_hook=unique_mapping, parse_constant=reject_constant
    )
    value = mapping(raw)
    expected: dict[str, object] = {
        "schema": 1,
        "catalogue": "calibration",
        "family": family,
        "warmup": WARMUP,
        "samples": SAMPLES,
        "reference": reference_case().metadata(),
        "completion": "cel-cost-family-complete",
    }
    if set(value) != set(expected) | {"cases", "resources"} or any(
        json.dumps(value[key], sort_keys=True) != json.dumps(item, sort_keys=True)
        for key, item in expected.items()
    ):
        raise ValueError("cost family identity does not match its manifest")
    cases = [
        operation_case(name, size)
        for name, size in OPERATION_IDENTITIES
        if name == family
    ]
    observations = sequence(value["cases"])
    if not cases or len(cases) != len(observations):
        raise ValueError("cost family is incomplete")
    for case, raw_observation in zip(cases, observations, strict=True):
        observation = mapping(raw_observation)
        prepared = bind(case.checked(), case.bindings, case.types)
        assert isinstance(prepared, BoundValues)
        metadata = {**case.metadata(), "prepared_sha256": digest(prepared.values)}
        if set(observation) != set(metadata) | {"target", "control_samples"} or any(
            json.dumps(observation[key], sort_keys=True)
            != json.dumps(item, sort_keys=True)
            for key, item in metadata.items()
        ):
            raise ValueError("cost observation does not match its manifest")
        for name in ("target", "control_samples"):
            samples = [mapping(item) for item in sequence(observation[name])]
            if len(samples) != WARMUP + SAMPLES:
                raise ValueError("incomplete cost samples")
            for sample in samples:
                if set(sample) != {
                    "elapsed_ns",
                    "calls",
                    "result_count",
                    "reference_before_ns",
                    "reference_after_ns",
                    "reference_calls",
                    "reference_before_results",
                    "reference_after_results",
                } or any(
                    type(item) is not int or item <= 0 for item in sample.values()
                ):
                    raise ValueError("invalid bracketed cost sample")
                if (
                    sample["calls"] != case.batch
                    or sample["result_count"] != case.batch
                    or any(
                        sample[field] != 32
                        for field in (
                            "reference_calls",
                            "reference_before_results",
                            "reference_after_results",
                        )
                    )
                ):
                    raise ValueError("cost sample call count changed")
    resources = mapping(value["resources"])
    if set(resources) != {
        "pid",
        "elapsed_ns",
        "descriptors_before",
        "descriptors_after",
        "persistent_workers",
        "stack_restored",
    }:
        raise ValueError("incomplete cost resource counters")
    for field in ("pid", "elapsed_ns", "descriptors_before", "descriptors_after"):
        item = resources[field]
        if type(item) is not int or item <= 0:
            raise ValueError("invalid cost resource counter")
    if (
        resources["descriptors_before"] != resources["descriptors_after"]
        or resources["stack_restored"] is not True
        or type(resources["persistent_workers"]) is not int
        or resources["persistent_workers"] != (1 if family == "evaluation" else 0)
        or cast(int, resources["elapsed_ns"]) >= 10_000_000_000
    ):
        raise ValueError("cost child did not release its owned resources")
    return cast(CostFamilyReport, value)


def validate_memory_observation(
    content: bytes, family: str, size: int
) -> dict[str, object]:
    raw: object = json.loads(
        content, object_pairs_hook=unique_mapping, parse_constant=reject_constant
    )
    value = mapping(raw)
    case = memory_case(family, size)
    metadata = case.metadata()
    expected: dict[str, object] = {
        "schema": 1,
        "catalogue": "cold_memory",
        **metadata,
        "completion": "cel-cold-memory-complete",
        "stack_restored": True,
        "original_statistics": list(binding_statistics(case.bindings)),
        "logical_bytes": logical_memory(case, case.checked()),
        "result_sha256": metadata["expected_sha256"],
        "resources": {
            "handles": 0,
            "outstanding": 0,
            "charged_bytes": 0,
            "persistent_workers": 1,
        },
    }
    fields = {
        "baseline",
        "reset",
        "measured",
        "linux_peak_reset",
        "production_ns",
        "pid",
        "elapsed_ns",
        "descriptors_before",
        "descriptors_after",
    }
    if set(value) != set(expected) | fields or any(
        json.dumps(value[key], sort_keys=True) != json.dumps(item, sort_keys=True)
        for key, item in expected.items()
    ):
        raise ValueError("memory observation does not match its manifest")
    for field in (
        "pid",
        "elapsed_ns",
        "production_ns",
        "descriptors_before",
        "descriptors_after",
    ):
        item = value[field]
        if type(item) is not int or item <= 0:
            raise ValueError("invalid memory process counter")
    logical = value["logical_bytes"]
    if type(logical) is not int or logical < 0:
        raise ValueError("invalid logical memory size")
    if (
        value["descriptors_before"] != value["descriptors_after"]
        or cast(int, value["elapsed_ns"]) >= 10_000_000_000
    ):
        raise ValueError("memory child did not release its descriptors")
    reset = value["linux_peak_reset"]
    if type(reset) is not bool:
        raise ValueError("invalid memory reset status")
    for field in ("baseline", "reset", "measured"):
        reading = value[field]
        if not reset:
            if reading is not None:
                raise ValueError("non-Linux memory cannot qualify")
        else:
            rss = mapping(reading)
            if set(rss) != {"current", "peak"} or any(
                type(item) is not int or item <= 0 for item in rss.values()
            ):
                raise ValueError("invalid memory RSS reading")
            if cast(int, rss["peak"]) < cast(int, rss["current"]):
                raise ValueError("memory peak is below its current reading")
    return value


def validate_observation(
    content: bytes, case: ConversionCase, mode: str
) -> dict[str, object]:
    raw: object = json.loads(
        content, object_pairs_hook=unique_mapping, parse_constant=reject_constant
    )
    value = mapping(raw)
    if mode not in ("full", "native"):
        raise ValueError("unknown observation mode")
    names = FULL_TIMINGS if mode == "full" else NATIVE_TIMINGS
    expected: dict[str, object] = {
        "schema": 2,
        "case": case.name,
        "mode": mode,
        **case.metadata(),
        "warmup": WARMUP,
        "samples": SAMPLES,
        "calls": {name: 1 if mode == "full" else case.batch for name in names},
        "stack_restored": True,
        "completion": "cel-conversion-complete",
    }
    if set(value) != set(expected) | {"timings", "resources"}:
        raise ValueError("unexpected observation fields")
    if any(
        json.dumps(value[key], sort_keys=True) != json.dumps(item, sort_keys=True)
        for key, item in expected.items()
    ):
        raise ValueError("observation does not match the declared workload")
    timings = timings_of(value)
    if set(timings) != set(names):
        raise ValueError("observation has the wrong timing phases")
    resources = mapping(value["resources"])
    required_resources = (
        {"handles": 0, "outstanding": 0, "charged_bytes": 0, "persistent_workers": 1}
        if mode == "full"
        else {"persistent_workers": 0}
    )
    if resources != required_resources or any(
        type(item) is not int for item in resources.values()
    ):
        raise ValueError("observation did not release its owned state")
    return value


def checked_process(argv: list[str]) -> ProcessResult:
    result = run_process(argv)
    if result.code != 0 or result.timed_out or result.overflow or result.stderr:
        raise RuntimeError("measurement subprocess did not complete cleanly")
    return result


def git_value(*arguments: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required for measurement provenance")
    return checked_process([git, "-C", str(ROOT), *arguments]).stdout.decode().strip()


def wheel_identity() -> dict[str, object]:
    distribution = importlib.metadata.distribution("common-expression-language")
    wheel_metadata = distribution.read_text("WHEEL")
    if wheel_metadata is None:
        raise RuntimeError("installed wheel metadata is missing")
    tags = {
        line.removeprefix("Tag: ")
        for line in wheel_metadata.splitlines()
        if line.startswith("Tag: ")
    }
    lock: object = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = sequence(mapping(lock)["package"])
    package = next(
        mapping(item)
        for item in packages
        if mapping(item).get("name") == "common-expression-language"
    )
    if distribution.version != package["version"]:
        raise RuntimeError("installed binding does not match the locked version")
    candidates: list[dict[str, object]] = []
    for raw in sequence(package["wheels"]):
        entry = mapping(raw)
        url = entry["url"]
        if not isinstance(url, str):
            raise ValueError("invalid wheel URL")
        filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        parts = filename.removesuffix(".whl").rsplit("-", 3)[-3:]
        supported = {
            "-".join(tag) for tag in itertools.product(*(p.split(".") for p in parts))
        }
        if tags & supported:
            candidates.append({"filename": filename, "locked_sha256": entry["hash"]})
    if len(candidates) != 1:
        raise RuntimeError("installed wheel does not have one matching locked archive")
    native: dict[str, str] = {}
    for path in distribution.files or ():
        if str(path).startswith("cel/") and str(path).endswith(".so"):
            native[str(path)] = hashlib.sha256(
                Path(str(distribution.locate_file(path))).read_bytes()
            ).hexdigest()
    if not native:
        raise RuntimeError("installed native wheel image is missing")
    return {
        "version": distribution.version,
        "archive": candidates[0],
        "installed_native_sha256": native,
        "tags": sorted(tags),
    }


def cpu_model() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("model name"):
                return line.partition(":")[2].strip()
    return platform.processor() or platform.machine()


def is_reference() -> bool:
    return (
        sys.platform == "linux"
        and platform.machine() == "x86_64"
        and sys.version_info[:3] == (3, 11, 17)
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("ImageOS") == "ubuntu24"
    )


def collect_conversion(output: Path, catalogue: str) -> int:
    started = time.monotonic()
    report = new_report()
    observations: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    candidates: list[dict[str, object]] = []
    manifest: list[str] = []
    header: dict[str, object] = {
        "source_revision": os.environ.get("KORA_MEASUREMENT_REVISION"),
        "checkout_revision": None,
        "tree": None,
        "dirty": None,
        "wheel": None,
    }
    selected: tuple[int, int] | None = None
    reference = False
    status = "measurement_failed"
    phase = "provenance"
    expected_count = 120 if catalogue == "bounds" else 28
    try:
        report.environment.update(
            {
                "cpu_model": cpu_model(),
                "machine": platform.machine(),
                "kernel": platform.release(),
                "runner_image": os.environ.get("ImageOS"),
                "image_version": os.environ.get("ImageVersion"),
                "python_build": str(sysconfig.get_config_var("CONFIG_ARGS")),
                "github_run_id": os.environ.get("GITHUB_RUN_ID"),
                "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "github_job": os.environ.get("GITHUB_JOB"),
                "runner_name": os.environ.get("RUNNER_NAME"),
            }
        )
        if catalogue not in ("pilot", "bounds"):
            raise ValueError("unknown conversion catalogue")
        reference = is_reference()
        header["source_revision"] = header["source_revision"] or git_value(
            "rev-parse", "HEAD"
        )
        header["checkout_revision"] = git_value("rev-parse", "HEAD")
        header["tree"] = git_value("rev-parse", "HEAD^{tree}")
        header["dirty"] = bool(git_value("status", "--porcelain"))
        header["wheel"] = wheel_identity()
        phase = "reference_qualification"
        if catalogue == "bounds" and not reference:
            raise RuntimeError("bound selection requires the reference runner")
        if os.environ.get("GITHUB_ACTIONS") == "true" and (
            not reference
            or header["dirty"]
            or header["source_revision"] != header["checkout_revision"]
        ):
            raise RuntimeError(
                "CI collection requires a clean matching reference checkout"
            )
        phase = "catalogue"
        cases = (
            tuple(
                bound_case(size, count, family)
                for size, count in BOUND_PAIRS
                for family in BOUND_FAMILIES
            )
            if catalogue == "bounds"
            else tuple(case.workload() for case in pilot_cases())
        )
        manifest = [case.name for case in cases]
        if len(manifest) != expected_count or len(set(manifest)) != expected_count:
            raise ValueError("conversion catalogue is incomplete or duplicated")
        minima: dict[tuple[int, int, str], int] = {}
        for case in cases:
            paired: dict[str, dict[str, list[int]]] = {}
            for mode in ("full", "native"):
                phase = case.name + "/" + mode
                remaining = PILOT_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    raise RuntimeError(
                        "conversion collection exceeded its suite deadline"
                    )
                result = run_process(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        PRELUDE,
                        json.dumps(full_path(ROOT)),
                        case.name,
                        mode,
                        catalogue,
                    ],
                    deadline_seconds=min(10.0, remaining),
                )
                failure: dict[str, object] = {
                    "case": case.name,
                    "mode": mode,
                    "code": result.code,
                    "timed_out": result.timed_out,
                    "overflow": result.overflow,
                    "stdout": result.stdout.decode("utf-8", errors="replace"),
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                }
                if (
                    result.code != 0
                    or result.timed_out
                    or result.overflow
                    or result.stderr
                ):
                    failures.append(failure)
                    raise RuntimeError("conversion child did not complete cleanly")
                try:
                    observation = validate_observation(result.stdout, case, mode)
                except (ValueError, UnicodeDecodeError):
                    failures.append(failure)
                    raise
                observations.append(observation)
                timings = timings_of(observation)
                paired[mode] = timings
                for name, times in timings.items():
                    for iteration, duration in enumerate(times):
                        report.samples.append(
                            Sample(
                                f"{case.name}/{name}",
                                iteration,
                                1 if mode == "full" else case.batch,
                                duration / 1_000_000_000,
                                {"nanoseconds": duration, "warmup": iteration < WARMUP},
                            )
                        )
            phase = case.name + "/decision"
            full = paired["full"]
            native = paired["native"]
            combined = conversion_samples(
                full["bind_ns"], native["native_ns"], native["control_ns"], case.batch
            )
            native_deltas = [
                actual - control
                for actual, control in zip(
                    native["native_ns"], native["control_ns"], strict=True
                )
            ]
            minimum = min(combined[WARMUP:])
            decisions.append(
                {
                    "case": case.name,
                    "byte_limit": case.byte_limit,
                    "value_limit": case.value_limit,
                    "native_calls": case.batch,
                    "combined_conversion_ns": combined,
                    "native_batch_delta_ns": native_deltas,
                    "native_per_call_ns": [
                        (value + case.batch - 1) // case.batch
                        for value in native["native_ns"]
                    ],
                    "control_per_call_ns": [
                        value // case.batch for value in native["control_ns"]
                    ],
                    "minimum_conversion_ns": minimum,
                    "minimum_preparation_ns": min(full["bind_ns"][WARMUP:]),
                    "minimum_production_ns": min(full["production_ns"][WARMUP:]),
                    "within_quarter_budget": minimum <= STOP_NS,
                }
            )
            if catalogue == "bounds":
                minima[
                    case.byte_limit, case.value_limit, case.name.rsplit("-", 2)[0]
                ] = minimum
        phase = "selection"
        if time.monotonic() - started > PILOT_SECONDS:
            raise RuntimeError("conversion collection exceeded its suite deadline")
        if catalogue == "bounds":
            selected = select_bounds(minima)
            candidates = [
                {
                    "byte_limit": size,
                    "value_limit": count,
                    "worst_minimum_ns": max(
                        minima[size, count, family] for family in BOUND_FAMILIES
                    ),
                    "within_headroom": 3
                    * max(minima[size, count, family] for family in BOUND_FAMILIES)
                    <= 2 * STOP_NS,
                }
                for size, count in BOUND_PAIRS
            ]
            status = (
                "bounds_selected" if selected is not None else "no_qualifying_bounds"
            )
        else:
            status = (
                "non_reference"
                if not reference
                else "passed"
                if all(item["within_quarter_budget"] for item in decisions)
                else "conversion_stop"
            )
        phase = "complete"
    finally:
        elapsed = time.monotonic() - started
        if elapsed > PILOT_SECONDS and status != "measurement_failed":
            status = "measurement_failed"
            selected = None
            phase = "suite_deadline"
        if status == "measurement_failed":
            failures.append({"phase": phase, "status": "incomplete"})
        report.durations["conversion"] = elapsed
        report.gates["suite_seconds"] = elapsed <= PILOT_SECONDS
        report.gates["all_cases_complete"] = (
            len(decisions) == expected_count and not failures
        )
        report.gates["reference_runner"] = reference
        report.gates["selection_qualified"] = (
            status == "bounds_selected" if catalogue == "bounds" else status == "passed"
        )
        payload: dict[str, object] = {
            "schema": 2,
            "catalogue": catalogue,
            "status": status,
            "phase": phase,
            "reference_runner": reference,
            **header,
            "stop_nanoseconds": STOP_NS,
            "warmup": WARMUP,
            "samples": SAMPLES,
            "manifest": manifest,
            "observations": observations,
            "decisions": decisions,
            "candidates": candidates,
            "selected_bounds": list(selected) if selected is not None else None,
            "failures": failures,
            "measurement": json.loads(report.to_json()),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if status == "measurement_failed":
        return 1
    return 3 if status in ("conversion_stop", "no_qualifying_bounds") else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("pilot", "select-bounds"))
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    output: object = arguments.output
    if not isinstance(output, Path):
        raise TypeError("measurement output must be a path")
    raise SystemExit(
        collect_conversion(
            output, "bounds" if arguments.command == "select-bounds" else "pilot"
        )
    )


if __name__ == "__main__":
    main()
