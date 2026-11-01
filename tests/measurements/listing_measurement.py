import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pydantic import TypeAdapter

from kora.ir.json import JsonObject, JsonObjectField, JsonValue
from tests.measurements.lifetime_cohorts import Cohort, run_cohorts
from tests.measurements.listing_cold import measure_cold_child
from tests.measurements.listing_population import (
    DATASET_COUNTS,
    EXTERNAL_COUNTS,
    declared_entries,
    fixture_entries,
    installed_entries,
    verify_inventory,
)
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
        for external, count in zip(EXTERNAL_COUNTS, DATASET_COUNTS, strict=True):
            record: JsonObject = {
                "entries": count,
                "fixture_entries": external,
                "fixture_schema_bytes": external * 8192,
                "declared_inventory": [
                    entry.model_dump(mode="json")
                    for entry in declared_entries(external)
                ],
                "observed_inventory": [],
                "verified_inventory": [],
                "inventory_verified": False,
            }
            datasets.append(record)
            publish_listing(sidecar, datasets, output, started, deadline)
            dataset = folder / str(count)
            dataset.mkdir()
            control = Path(
                tempfile.mkdtemp(prefix="kora-list-measure-", dir="/tmp")
            ).resolve()
            (dataset / "control-root.txt").write_text(str(control))
            entries = fixture_entries(external)
            setup_started = time.monotonic()
            wheel = build_fixture_wheel(
                dataset, entries, {"socket": str(control)}, inventory=external - 1
            )
            python = install_measurement_fixture(dataset, wheel)
            observed = installed_entries(python)
            record["wheel_bytes"] = wheel.stat().st_size
            record["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
            record["observed_inventory"] = [
                entry.model_dump(mode="json") for entry in observed
            ]
            verify_inventory(external, observed)
            record["verified_inventory"] = record["observed_inventory"]
            record["inventory_verified"] = True
            publish_listing(sidecar, datasets, output, started, deadline)
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
            record["setup_seconds"] = setup_seconds
            record["parents"] = [parent.model_dump(mode="json") for parent in parents]
        sidecar["all_420_cycles"] = total == 420
        assert total == 420
        assert time.monotonic() < deadline
    finally:
        publish_listing(sidecar, datasets, output, started, deadline)


def publish_listing(
    sidecar: JsonObject,
    datasets: list[JsonValue],
    output: Path,
    started: float,
    deadline: float,
) -> None:
    sidecar["datasets"] = datasets
    sidecar["seconds"] = time.monotonic() - started
    sidecar["remaining_qualification_seconds"] = deadline - time.monotonic()
    (output.parent / "listing.json").write_text(json.dumps(sidecar, sort_keys=True))
    if time.monotonic() >= deadline and sys.exception() is None:
        raise TimeoutError(
            "listing evidence publication exceeded qualification deadline"
        )
