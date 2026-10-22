import base64
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import cast

import pytest

from tests.cel_eval.children import ROOT, full_path
from tests.measurements.cel_cost_smoke import (
    CASE_NAMES,
    PRELUDE,
    TARGET_NS,
    SmokeSample,
    smoke_case,
    validate_smoke,
)
from tests.measurements.cel_measurement_process import run_process
from tests.measurements.harness import Sample, new_report


def checked_output(arguments: list[str]) -> str:
    result = run_process(arguments)
    if result.code or result.stderr or result.timed_out or result.overflow:
        raise RuntimeError("measurement provenance command failed")
    return result.stdout.decode().strip()


def provenance() -> dict[str, object]:
    git = shutil.which("git")
    uv = shutil.which("uv")
    if git is None or uv is None:
        raise RuntimeError("measurement requires git and uv")
    distribution = importlib.metadata.distribution("common-expression-language")
    native = {
        str(path): hashlib.sha256(
            Path(str(distribution.locate_file(path))).read_bytes()
        ).hexdigest()
        for path in distribution.files or ()
        if str(path).startswith("cel/") and str(path).endswith(".so")
    }
    if not native:
        raise RuntimeError("measurement requires a native binding image")
    return {
        "checkout_revision": checked_output(
            [git, "-C", str(ROOT), "rev-parse", "HEAD"]
        ),
        "tree": checked_output([git, "-C", str(ROOT), "rev-parse", "HEAD^{tree}"]),
        "dirty": bool(checked_output([git, "-C", str(ROOT), "status", "--porcelain"])),
        "python": sys.version,
        "uv": checked_output([uv, "--version"]),
        "binding_version": distribution.version,
        "native_sha256": native,
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "machine": platform.machine(),
        "kernel": platform.release(),
        "runner_image": os.environ.get("ImageOS"),
        "image_version": os.environ.get("ImageVersion"),
        "runner_name": os.environ.get("RUNNER_NAME"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
    }


@pytest.mark.measurement
def test_cel_cost_regression_within_target(tmp_path: Path) -> None:
    started = time.monotonic()
    report = new_report()
    output = Path(
        os.environ.get("KORA_CEL_COST_OUTPUT", str(tmp_path / "cel-cost-smoke.json"))
    )
    raw: list[dict[str, object]] = []
    header: dict[str, object] = {}
    report.gates["all_cases_complete"] = False
    reference = (
        sys.platform == "linux"
        and platform.machine() == "x86_64"
        and sys.version_info[:3] == (3, 11, 17)
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("ImageOS") == "ubuntu24"
    )
    try:
        header = provenance()
        if os.environ.get("GITHUB_ACTIONS") == "true":
            assert reference
            assert header["checkout_revision"] == report.revision
            assert header["dirty"] is False
        for name in CASE_NAMES:
            remaining = 240 - (time.monotonic() - started)
            assert remaining > 0
            result = run_process(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    PRELUDE,
                    json.dumps(full_path(ROOT)),
                    name,
                ],
                deadline_seconds=min(10, remaining),
            )
            raw.append(
                {
                    "case": name,
                    "pid": result.pid,
                    "code": result.code,
                    "timed_out": result.timed_out,
                    "overflow": result.overflow,
                    "stdout_base64": base64.b64encode(result.stdout).decode(),
                    "stderr_base64": base64.b64encode(result.stderr).decode(),
                }
            )
            assert result.code == 0
            assert not result.timed_out
            assert not result.overflow
            assert not result.stderr
            observation = validate_smoke(result.stdout, smoke_case(name))
            assert observation["pid"] == result.pid
            samples = cast(list[SmokeSample], observation["samples"])
            for iteration, sample in enumerate(samples):
                report.samples.append(
                    Sample(
                        name,
                        iteration,
                        1,
                        sample["nanoseconds"] / 1_000_000_000,
                        {
                            "nanoseconds": sample["nanoseconds"],
                            "phase": sample["phase"],
                        },
                    )
                )
            report.gates[name] = all(
                sample["nanoseconds"] <= TARGET_NS for sample in samples
            )
            assert report.gates[name], name
        report.gates["all_cases_complete"] = len(raw) == len(CASE_NAMES)
    finally:
        report.durations["cost_smoke"] = time.monotonic() - started
        report.gates["suite_seconds"] = report.durations["cost_smoke"] <= 240
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "status": "reference" if reference else "non_reference",
                    "target_nanoseconds": TARGET_NS,
                    "calls_per_case": 4,
                    "manifest": [smoke_case(name).metadata() for name in CASE_NAMES],
                    "provenance": header,
                    "raw_children": raw,
                    "measurement": json.loads(report.to_json()),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    assert all(report.gates.values())
