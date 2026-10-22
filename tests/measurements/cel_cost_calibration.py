import argparse
import hashlib
import importlib.metadata
import itertools
import json
import os
import platform
import shutil
import sys
import sysconfig
import time
import tomllib
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlsplit

from tests.cel_eval.children import full_path
from tests.measurements.cel_bound_workload import (
    BOUND_FAMILIES,
    BOUND_PAIRS,
    bound_case,
    conversion_samples,
    select_bounds,
)
from tests.measurements.cel_cost_workload import (
    FULL_TIMINGS,
    NATIVE_TIMINGS,
    SAMPLES,
    WARMUP,
    ConversionCase,
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
