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
from tests.measurements.cel_cost_workload import (
    FULL_TIMINGS,
    NATIVE_TIMINGS,
    SAMPLES,
    WARMUP,
    PilotCase,
    pilot_cases,
    prepared,
)
from tests.measurements.cel_measurement_process import ProcessResult, run_process
from tests.measurements.harness import Sample, new_report

ROOT: Path = Path(__file__).resolve().parents[2]
STOP_NS: int = 2_500_000
PILOT_SECONDS: float = 240.0
PRELUDE: str = (
    "import json, sys; sys.path[:] = json.loads(sys.argv[1]); "
    "from tests.measurements.cel_cost_workload import run_case; "
    "run_case(sys.argv[2], sys.argv[3])"
)
OBSERVATION_KEYS: frozenset[str] = frozenset(
    {
        "schema",
        "case",
        "mode",
        "input_sha256",
        "original_bytes",
        "converted_bytes",
        "values",
        "warmup",
        "samples",
        "timings",
        "resources",
        "stack_restored",
        "completion",
    }
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


def validate_observation(
    content: bytes, case: PilotCase, mode: str
) -> dict[str, object]:
    raw: object = json.loads(content)
    value = mapping(raw)
    if frozenset(value) != OBSERVATION_KEYS:
        raise ValueError("unexpected observation fields")
    converted = prepared(case)
    expected: dict[str, object] = {
        "schema": 1,
        "case": case.name,
        "mode": mode,
        "input_sha256": case.digest(),
        "original_bytes": case.original_size().bytes,
        "converted_bytes": converted.bytes,
        "values": converted.count,
        "warmup": WARMUP,
        "samples": SAMPLES,
        "stack_restored": True,
        "completion": "cel-conversion-complete",
    }
    if any(
        type(value[key]) is not type(item) or value[key] != item
        for key, item in expected.items()
    ):
        raise ValueError("observation does not match the declared workload")
    timings = timings_of(value)
    names = FULL_TIMINGS if mode == "full" else NATIVE_TIMINGS
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


def pilot(output: Path) -> int:
    started = time.monotonic()
    report = new_report()
    report.environment.update(
        {
            "cpu_model": cpu_model(),
            "machine": platform.machine(),
            "kernel": platform.release(),
            "runner_image": os.environ.get("ImageOS"),
            "image_version": os.environ.get("ImageVersion"),
            "python_build": str(sysconfig.get_config_var("CONFIG_ARGS")),
        }
    )
    observations: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    reference = is_reference()
    status = "measurement_failed"
    source = os.environ.get("KORA_MEASUREMENT_REVISION") or git_value(
        "rev-parse", "HEAD"
    )
    checkout = git_value("rev-parse", "HEAD")
    tree = git_value("rev-parse", "HEAD^{tree}")
    dirty = bool(git_value("status", "--porcelain"))
    wheel = wheel_identity()
    try:
        if os.environ.get("GITHUB_ACTIONS") == "true" and (
            not reference or dirty or source != checkout
        ):
            raise RuntimeError("CI pilot requires a clean matching reference checkout")
        for case in pilot_cases():
            paired: dict[str, dict[str, list[int]]] = {}
            for mode in ("full", "native"):
                if time.monotonic() - started >= PILOT_SECONDS:
                    raise RuntimeError("conversion pilot exceeded its suite deadline")
                result = run_process(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        PRELUDE,
                        json.dumps(full_path(ROOT)),
                        case.name,
                        mode,
                    ]
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
                                1,
                                duration / 1_000_000_000,
                                {"nanoseconds": duration, "warmup": iteration < WARMUP},
                            )
                        )
            full = paired["full"]
            native = paired["native"]
            combined = [
                prep + actual - control
                for prep, actual, control in zip(
                    full["bind_ns"],
                    native["native_ns"],
                    native["control_ns"],
                    strict=True,
                )
            ]
            native_deltas = [
                actual - control
                for actual, control in zip(
                    native["native_ns"], native["control_ns"], strict=True
                )
            ]
            if min(native_deltas[WARMUP:]) <= 0:
                raise RuntimeError("native conversion contribution is not identifiable")
            minimum = min(combined[WARMUP:])
            decisions.append(
                {
                    "case": case.name,
                    "combined_conversion_ns": combined,
                    "native_conversion_delta_ns": native_deltas,
                    "minimum_conversion_ns": minimum,
                    "minimum_preparation_ns": min(full["bind_ns"][WARMUP:]),
                    "minimum_production_ns": min(full["production_ns"][WARMUP:]),
                    "within_quarter_budget": minimum <= STOP_NS,
                }
            )
        if time.monotonic() - started > PILOT_SECONDS:
            raise RuntimeError("conversion pilot exceeded its suite deadline")
        status = (
            "non_reference"
            if not reference
            else "passed"
            if all(item["within_quarter_budget"] for item in decisions)
            else "conversion_stop"
        )
    finally:
        elapsed = time.monotonic() - started
        report.durations["pilot"] = elapsed
        report.gates["suite_seconds"] = elapsed <= PILOT_SECONDS
        report.gates["all_cases_complete"] = len(decisions) == 28
        report.gates["reference_runner"] = reference
        report.gates["conversion_within_quarter_budget"] = len(decisions) == 28 and all(
            item["within_quarter_budget"] for item in decisions
        )
        payload: dict[str, object] = {
            "schema": 1,
            "status": status,
            "reference_runner": reference,
            "source_revision": source,
            "checkout_revision": checkout,
            "tree": tree,
            "dirty": dirty,
            "wheel": wheel,
            "stop_nanoseconds": STOP_NS,
            "warmup": WARMUP,
            "samples": SAMPLES,
            "observations": observations,
            "decisions": decisions,
            "failures": failures,
            "measurement": json.loads(report.to_json()),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 3 if status == "conversion_stop" else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("pilot",))
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    output: object = arguments.output
    if not isinstance(output, Path):
        raise TypeError("measurement output must be a path")
    raise SystemExit(pilot(output))


if __name__ == "__main__":
    main()
