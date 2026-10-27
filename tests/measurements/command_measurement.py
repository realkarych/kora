import asyncio
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from pydantic import TypeAdapter

from kora.ir.json import JsonObject, JsonObjectField
from tests.measurements.command_workload import SEQUENCE
from tests.measurements.lifetime_cohorts import Cohort, run_cohorts
from tests.measurements.lifetime_evidence import (
    EvidenceError,
    EvidenceItem,
    EvidenceReason,
    EvidenceState,
    StepOutcome,
    cohort_evidence,
    write_receipt,
)
from tests.measurements.lifetime_measurement_support import HELPER_MEASUREMENT
from tests.measurements.lifetime_observation import Point
from tests.measurements.lifetime_workload import OriginalResult
from tests.measurements.listing_measurement import (
    QUALIFICATION_DEADLINE,
    source_receipt,
)
from tests.support.provider_tooling import (
    build_fixture_wheel,
    install_measurement_fixture,
)


def validate_parent(folder: Path) -> int:
    result = TypeAdapter(JsonObjectField).validate_json(
        (folder / "stdout.json").read_bytes()
    )
    cycles = result["cycles"]
    assert isinstance(cycles, list)
    assert len(cycles) == len(SEQUENCE) == 90
    assert [cycle["workload"] for cycle in cycles if isinstance(cycle, dict)] == list(
        SEQUENCE
    )
    for index, cycle in enumerate(cycles):
        assert isinstance(cycle, dict)
        assert cycle["warmup"] is (index < 10)
        assert cycle["released"] is True
        assert (folder / f"cycle-{index}" / "result.json").is_file()
        if cycle["workload"] == "recovery":
            original = OriginalResult.model_validate(cycle["original_parent"])
            assert original.heap_peak_bytes > 0
            assert original.native_peak_bytes > 0
            assert not original.unobserved_pids
            original_helpers = tuple(
                HELPER_MEASUREMENT.validate_json(line)
                for line in (folder / f"cycle-{index}" / "original-helpers.jsonl")
                .read_bytes()
                .splitlines()
            )
            assert len(original_helpers) == original.helpers
            assert all(helper.code == 0 for helper in original_helpers)
    points = tuple(
        Point.model_validate_json(line)
        for line in (folder / "points.jsonl").read_bytes().splitlines()
    )
    assert len(points) == result["samples"]
    helpers = tuple(
        HELPER_MEASUREMENT.validate_json(line)
        for line in (folder / "helpers.jsonl").read_bytes().splitlines()
    )
    assert len(helpers) == result["helpers"]
    assert all(helper.code == 0 for helper in helpers)
    settled = [point for point in points if point.phase == "settled"]
    assert len(settled) == 90
    for point in settled:
        assert point.absent_processes == point.tracked_processes
        assert point.unknown_processes == 0
        assert not point.child_rss
        assert not any(point.recovery.values())
    return len(cycles)


def measure_command(output: Path) -> None:
    started = time.monotonic()
    deadline = QUALIFICATION_DEADLINE
    folder = output.parent / "command-artifacts"
    folder.mkdir(mode=0o700)
    (folder / "source.json").write_text(json.dumps(source_receipt(), sort_keys=True))
    result: JsonObject = {
        "revision": os.environ["KORA_MEASUREMENT_REVISION"],
        "source_manifest_sha256": hashlib.sha256(
            (folder / "source.json").read_bytes()
        ).hexdigest(),
        "build_mode": "locked editable core and installed fixture wheel",
        "parents": 3,
        "warmup_per_parent": 10,
        "observed_per_parent": 80,
        "sample_interval_seconds": 0.02,
        "parent_trace_frames": 1,
        "native_peaks": "sampled lower bounds; unavailable child counters remain null",
        "child_trace_start": "explicit fixture module import",
        "all_270_cycles": False,
    }
    cohorts: list[Cohort] = []
    removed: set[int] = set()
    try:
        wheel = build_fixture_wheel(
            folder,
            {
                "secrets.configured": (
                    "kora_tooling_fixture.configured_secrets:configured"
                )
            },
        )
        python = install_measurement_fixture(folder, wheel)
        for index in range(3):
            parent = folder / str(index)
            parent.mkdir(mode=0o700)
            home = Path(tempfile.mkdtemp(prefix="kora-life-", dir="/tmp")).resolve()
            (parent / "home.txt").write_text(str(home))
            cohorts.append(
                Cohort(
                    arguments=(
                        str(python),
                        "-X",
                        "tracemalloc=1",
                        "-m",
                        "tests.measurements.command_workload",
                        str(home),
                        str(parent),
                    ),
                    home=home,
                    folder=parent,
                )
            )
        parents = asyncio.run(run_cohorts(tuple(cohorts), deadline))
        assert all(parent.reaped and parent.returncode == 0 for parent in parents)
        assert sum(validate_parent(cohort.folder) for cohort in cohorts) == 270
        assert time.monotonic() < deadline
        for index, cohort in enumerate(cohorts):
            assert not tuple(cohort.home.rglob("journal"))
            assert not tuple(cohort.home.rglob("claim"))
            shutil.rmtree(cohort.home)
            removed.add(index)
        result["all_270_cycles"] = True
    finally:
        publish_command(
            result,
            output,
            cohorts,
            removed,
            started,
            deadline,
            failed=sys.exception() is not None,
        )


def publish_command(
    result: JsonObject,
    output: Path,
    cohorts: list[Cohort],
    removed: set[int],
    started: float,
    deadline: float,
    *,
    failed: bool,
) -> None:
    folder = output.parent / "command-artifacts"
    try:
        items: list[EvidenceItem] = []
        for index, cohort in enumerate(cohorts):
            try:
                item = cohort_evidence(
                    cohort.folder,
                    folder,
                    index,
                    StepOutcome.SUCCESS if index in removed else StepOutcome.FAILURE,
                    cohort.home.parent,
                )
            except EvidenceError as failure:
                item = EvidenceItem(index, EvidenceState.UNVERIFIED, failure.reason)
            except (OSError, tarfile.TarError):
                item = EvidenceItem(
                    index, EvidenceState.UNVERIFIED, EvidenceReason.UNAVAILABLE
                )
            items.append(item)
        result["retention_verified"] = write_receipt(folder, tuple(items))
        if not result["retention_verified"] and not failed:
            raise ValueError("command retention evidence unverified")
    finally:
        result["seconds"] = time.monotonic() - started
        result["remaining_qualification_seconds"] = deadline - time.monotonic()
        result["within_command_deadline"] = time.monotonic() < deadline
        (output.parent / "command.json").write_text(json.dumps(result, sort_keys=True))
        if time.monotonic() >= deadline and not failed and sys.exception() is None:
            raise TimeoutError(
                "command evidence publication exceeded qualification deadline"
            )
