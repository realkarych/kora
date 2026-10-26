import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from pydantic import TypeAdapter

from kora.ir.json import JsonObject, JsonObjectField, JsonValue
from tests.measurements.lifetime_cohorts import Cohort, run_cohorts
from tests.measurements.listing_cold import measure_cold_child
from tests.measurements.listing_workload import SEQUENCE
from tests.support.provider_tooling import (
    REPOSITORY,
    build_fixture_wheel,
    install_measurement_fixture,
)

QUALIFICATION_DEADLINE: float = time.monotonic() + 600


def source_receipt() -> dict[str, str]:
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("Git is required for source provenance")
    result = subprocess.run(
        [executable, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
        cwd=REPOSITORY,
        timeout=30,
    )
    return {
        name: hashlib.sha256((REPOSITORY / name).read_bytes()).hexdigest()
        for name in result.stdout.decode().split("\x00")
        if name and (REPOSITORY / name).is_file()
    }


def measure_listing(output: Path) -> None:
    started = time.monotonic()
    deadline = QUALIFICATION_DEADLINE
    folder = output.parent / "listing-artifacts"
    folder.mkdir(mode=0o700)
    receipt = source_receipt()
    (folder / "source.json").write_text(json.dumps(receipt, sort_keys=True))
    sidecar: JsonObject = {
        "revision": os.environ["KORA_MEASUREMENT_REVISION"],
        "source_manifest_sha256": hashlib.sha256(
            (folder / "source.json").read_bytes()
        ).hexdigest(),
        "build_mode": "locked editable core and installed fixture wheels",
        "datasets": [],
        "all_420_cycles": False,
    }
    datasets: list[JsonValue] = []
    total = 0
    try:
        for count in (5, 256):
            dataset = folder / str(count)
            dataset.mkdir()
            control = Path(
                tempfile.mkdtemp(prefix="kora-list-measure-", dir="/tmp")
            ).resolve()
            (dataset / "control-root.txt").write_text(str(control))
            entries = {
                f"policy.item_{index:03}": (
                    f"kora_tooling_fixture.inventory:item_{index:03}"
                )
                for index in range(count - 5)
            }
            entries["policy.zz_controlled"] = (
                "kora_tooling_fixture.measurement:controlled"
            )
            setup_started = time.monotonic()
            wheel = build_fixture_wheel(
                dataset, entries, {"socket": str(control)}, inventory=count - 5
            )
            python = install_measurement_fixture(dataset, wheel)
            cohorts: list[Cohort] = []
            for index in range(3):
                parent = dataset / str(index)
                parent.mkdir()
                cohorts.append(
                    Cohort(
                        arguments=(
                            str(python),
                            "-X",
                            "tracemalloc=1",
                            "-m",
                            "tests.measurements.listing_workload",
                            str(parent),
                            str(control),
                            str(count),
                        ),
                        home=control,
                        folder=parent,
                    )
                )
            setup_seconds = time.monotonic() - setup_started
            parents = asyncio.run(run_cohorts(tuple(cohorts), deadline))
            assert all(parent.reaped and parent.returncode == 0 for parent in parents)
            for cohort in cohorts:
                result = TypeAdapter(JsonObjectField).validate_json(
                    (cohort.folder / "stdout.json").read_bytes()
                )
                cycles = result["cycles"]
                assert isinstance(cycles, list)
                assert len(cycles) == len(SEQUENCE) == 70
                assert [
                    cycle["workload"] for cycle in cycles if isinstance(cycle, dict)
                ] == list(SEQUENCE)
                total += len(cycles)
            cold = asyncio.run(
                measure_cold_child(python, control, count, deadline, dataset)
            )
            (dataset / "cold.json").write_text(json.dumps(cold, sort_keys=True))
            control.rmdir()
            datasets.append(
                {
                    "entries": count,
                    "fixture_entries": count - 4,
                    "fixture_schema_bytes": (count - 4) * 8192,
                    "wheel_bytes": wheel.stat().st_size,
                    "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    "setup_seconds": setup_seconds,
                    "parents": [parent.model_dump(mode="json") for parent in parents],
                }
            )
        sidecar["all_420_cycles"] = total == 420
        assert total == 420
        assert time.monotonic() < deadline
    finally:
        sidecar["datasets"] = datasets
        sidecar["seconds"] = time.monotonic() - started
        sidecar["remaining_qualification_seconds"] = deadline - time.monotonic()
        (output.parent / "listing.json").write_text(json.dumps(sidecar, sort_keys=True))
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "listing evidence publication exceeded qualification deadline"
            )
