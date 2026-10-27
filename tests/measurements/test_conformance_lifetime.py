import asyncio
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.measurements.command_measurement import measure_command
from tests.measurements.harness import Report, Sample, new_report, write_report
from tests.measurements.lifetime_cohorts import Cohort, run_cohorts
from tests.measurements.lifetime_measurement_support import (
    HELPER_MEASUREMENT,
    ColdHelperMeasurement,
    MutationHelperMeasurement,
)
from tests.measurements.lifetime_observation import Point
from tests.measurements.lifetime_workload import SEQUENCE, RunResult
from tests.measurements.listing_measurement import measure_listing

pytestmark = pytest.mark.measurement


@pytest.fixture
def report() -> Iterator[Report]:
    result = new_report()
    yield result
    write_report(result)
    deadline = result.environment.get("command_deadline_monotonic")
    if isinstance(deadline, float) and time.monotonic() >= deadline:
        result.gates["within_command_deadline"] = False
        write_report(result)
        raise AssertionError("measurement report exceeded command deadline")


def test_conformance_fixture_resources_are_released(report: Report) -> None:
    output = Path(os.environ["KORA_MEASUREMENT_OUTPUT"])
    assert output.is_absolute()
    assert report.revision
    artifacts = output.with_suffix(".artifacts")
    artifacts.mkdir(mode=0o700, parents=False, exist_ok=False)
    deadline = time.monotonic() + 600
    report.environment.update(
        independent_parents=3,
        cycles_per_parent=80,
        warmup_per_parent=10,
        sample_interval_seconds=0.02,
        parent_trace_frames=1,
        helper_trace_frames=1,
        cold_helper_records_per_parent=1,
        helper_mutation_trace_start="immediately_before_main",
        direct_environment_bytes=440000,
        wire_environment_bytes=220000,
        artifacts=str(artifacts),
        cohort_scheduling="three_concurrent_independent_parents",
        command_deadline_seconds=600,
        normal_completion_deadline_seconds=595,
        shared_failure_guardian_seconds=5,
        command_deadline_monotonic=deadline,
    )

    cohorts: list[Cohort] = []
    for run in range(3):
        folder = artifacts / str(run)
        folder.mkdir(mode=0o700)
        private = str(Path(tempfile.mkdtemp(prefix="kora-life-", dir="/tmp")).resolve())
        (folder / "home.txt").write_text(private)
        cohorts.append(
            Cohort(
                arguments=(
                    sys.executable,
                    "-X",
                    "tracemalloc=1",
                    "-m",
                    "tests.measurements.lifetime_workload",
                    private,
                    str(folder),
                ),
                home=Path(private),
                folder=folder,
            )
        )
    parents = asyncio.run(run_cohorts(tuple(cohorts), deadline))
    assert len(parents) == 3
    assert all(parent.reaped and parent.returncode == 0 for parent in parents)
    for run, cohort in enumerate(cohorts):
        assert time.monotonic() < deadline
        folder = cohort.folder
        result = RunResult.model_validate_json((folder / "stdout.json").read_bytes())
        assert time.monotonic() < deadline
        helpers = tuple(
            HELPER_MEASUREMENT.validate_json(line)
            for line in (folder / "helpers.jsonl").read_bytes().splitlines()
        )
        points = tuple(
            Point.model_validate_json(line)
            for line in (folder / "points.jsonl").read_bytes().splitlines()
        )
        assert time.monotonic() < deadline
        assert tuple(cycle.workload for cycle in result.cycles) == SEQUENCE
        assert len(result.cycles) == 80
        assert len(result.settled) == 8
        assert len(result.retained_types) == 8
        assert len(helpers) == result.helpers
        cold = tuple(
            helper for helper in helpers if isinstance(helper, ColdHelperMeasurement)
        )
        mutations = tuple(
            helper
            for helper in helpers
            if isinstance(helper, MutationHelperMeasurement)
        )
        assert len(cold) == 1
        assert len(mutations) == result.helpers - 1
        assert len(points) == result.samples
        baseline = result.settled[0]
        for counts in result.retained_types:
            assert counts == result.retained_types[0]
        for settled in result.settled:
            assert settled.descriptors == baseline.descriptors
            assert settled.tasks == baseline.tasks
            assert settled.timers == baseline.timers
            assert settled.watches == baseline.watches
            assert settled.python_threads == baseline.python_threads
            assert settled.native_threads == baseline.native_threads
            assert settled.imports == baseline.imports
            assert not settled.child_rss
            assert settled.tracked_processes > 0
            assert settled.absent_processes == settled.tracked_processes
            assert settled.unknown_processes == 0
            assert not any(settled.recovery.values())
        for cycle in result.cycles:
            assert cycle.journal_removed
            assert cycle.owner_collected
            assert cycle.input_collected
            if cycle.workload == "recovery":
                assert cycle.original_parent_heap_peak_bytes is not None
                assert cycle.original_parent_heap_peak_bytes > 0
                assert cycle.original_parent_native_peak_bytes is not None
                assert cycle.original_parent_native_peak_bytes > 0
            else:
                assert cycle.original_parent_heap_peak_bytes is None
                assert cycle.original_parent_native_peak_bytes is None
            report.samples.append(
                Sample(
                    cycle.workload,
                    run * 80 + cycle.cycle,
                    1,
                    cycle.seconds,
                    {
                        "warmup": cycle.cycle < 10,
                        "helpers": cycle.helpers,
                        "captured_bytes": cycle.captured_bytes,
                        "process_code": cycle.process_code,
                        "original_parent_heap_peak_bytes": (
                            cycle.original_parent_heap_peak_bytes
                        ),
                        "original_parent_native_peak_bytes": (
                            cycle.original_parent_native_peak_bytes
                        ),
                        "settled": True,
                    },
                )
            )
        assert time.monotonic() < deadline
        shutil.rmtree(cohort.home)
        report.environment[f"parent_{run}_heap_peak_bytes"] = max(
            point.heap_peak_bytes for point in points
        )
        report.environment[f"parent_{run}_rss_peak_bytes"] = max(
            point.rss_bytes for point in points
        )
        report.environment[f"helper_{run}_cold_heap_peak_bytes"] = cold[
            0
        ].cold_peak_bytes
        report.environment[f"helper_{run}_mutation_heap_peak_bytes"] = max(
            helper.mutation_peak_bytes for helper in mutations
        )
        report.environment[f"helper_{run}_cold_records"] = len(cold)
        report.environment[f"helper_{run}_mutation_records"] = len(mutations)
        report.gates[f"parent_{run}_explicit_settlement"] = True

    report.gates["all_240_cycles"] = len(report.samples) == 240
    report.gates["within_command_deadline"] = time.monotonic() < deadline
    assert all(report.gates.values())


def test_installed_listing_resources_are_released() -> None:
    output = Path(os.environ["KORA_MEASUREMENT_OUTPUT"])

    measure_listing(output)

    assert (output.parent / "listing.json").is_file()


def test_installed_command_resources_are_released() -> None:
    output = Path(os.environ["KORA_MEASUREMENT_OUTPUT"])

    measure_command(output)

    assert (output.parent / "command.json").is_file()
