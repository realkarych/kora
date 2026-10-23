import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.measurements.harness import Report, Sample, report_lifecycle
from tests.measurements.lifetime_measurement_support import (
    HELPER_MEASUREMENT,
    ColdHelperMeasurement,
    MutationHelperMeasurement,
)
from tests.measurements.lifetime_observation import Point
from tests.measurements.lifetime_workload import SEQUENCE, RunResult

pytestmark = pytest.mark.measurement


@pytest.fixture
def report() -> Iterator[Report]:
    yield from report_lifecycle()


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
    )

    for run in range(3):
        folder = artifacts / str(run)
        folder.mkdir(mode=0o700)
        private = str(Path(tempfile.mkdtemp(prefix="kora-life-", dir="/tmp")).resolve())
        (folder / "home.txt").write_text(private)
        with (folder / "stdout.json").open("xb") as stdout:
            with (folder / "stderr.txt").open("xb") as stderr:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-X",
                        "tracemalloc=1",
                        "-m",
                        "tests.measurements.lifetime_workload",
                        private,
                        str(folder),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=max(0, deadline - time.monotonic()),
                    check=False,
                )
        assert completed.returncode == 0, (folder / "stderr.txt").read_text()
        result = RunResult.model_validate_json((folder / "stdout.json").read_bytes())
        helpers = tuple(
            HELPER_MEASUREMENT.validate_json(line)
            for line in (folder / "helpers.jsonl").read_bytes().splitlines()
        )
        points = tuple(
            Point.model_validate_json(line)
            for line in (folder / "points.jsonl").read_bytes().splitlines()
        )
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
        shutil.rmtree(private)
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
    assert all(report.gates.values())
