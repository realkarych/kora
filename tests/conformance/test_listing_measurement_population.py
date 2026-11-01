import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from kora.ir.json import JsonObjectField
from tests.measurements.listing_measurement import DATASET_COUNTS
from tests.measurements.listing_population import (
    InstalledEntry,
    declared_entries,
    fixture_entries,
    installed_entries,
    verify_inventory,
)
from tests.support.provider_tooling import (
    REPOSITORY,
    build_fixture_wheel,
    install_measurement_fixture,
)


@pytest.mark.parametrize(
    ("external", "count"), tuple(zip((1, 252), DATASET_COUNTS, strict=True))
)
@pytest.mark.parametrize("case", ["success", "failure"])
def test_listing_measurement_preserves_exact_discovery_outcomes(
    tmp_path: Path, external: int, count: int, case: str
) -> None:
    entries = {
        f"policy.item_{index:03}": f"kora_tooling_fixture.inventory:item_{index:03}"
        for index in range(external - 1)
    }
    entries["policy.zz_controlled"] = "kora_tooling_fixture.measurement:controlled"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    with tempfile.TemporaryDirectory(prefix="kora-population-", dir="/tmp") as private:
        control = Path(private).resolve()
        wheel = build_fixture_wheel(
            tmp_path, entries, {"socket": str(control)}, inventory=external - 1
        )
        python = install_measurement_fixture(tmp_path, wheel)

        completed = subprocess.run(
            [
                str(python),
                "-X",
                "tracemalloc=1",
                "-m",
                "tests.support.listing_population",
                str(evidence),
                str(control),
                str(count),
                case,
            ],
            cwd=REPOSITORY,
            capture_output=True,
            check=False,
            timeout=60,
        )

        assert completed.returncode == 0, completed.stderr.decode()
        assert not tuple(control.iterdir())


@pytest.mark.parametrize("external", [1, 252])
def test_listing_measurement_population_matches_installed_entries(
    tmp_path: Path, external: int
) -> None:
    wheel = build_fixture_wheel(
        tmp_path, fixture_entries(external), inventory=external - 1
    )
    python = install_measurement_fixture(tmp_path, wheel)

    observed = installed_entries(python)
    verify_inventory(external, observed)

    assert len(observed) == 9 + external
    assert sum(entry.distribution == "kora" for entry in observed) == 9
    assert (
        sum(entry.distribution == "kora-tooling-fixture" for entry in observed)
        == external
    )


@pytest.mark.parametrize("external", [1, 252])
@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "target", "owner"])
def test_listing_measurement_rejects_changed_inventory(
    external: int, change: str
) -> None:
    expected = declared_entries(external)
    first = expected[0]
    replacement = InstalledEntry(
        name=first.name if change != "extra" else "policy.unexpected",
        target=first.target if change != "target" else "kora.invalid:metadata",
        distribution=first.distribution if change != "owner" else "other-distribution",
    )
    observed = (
        expected[1:]
        if change == "missing"
        else expected + (first,)
        if change == "duplicate"
        else expected + (replacement,)
        if change == "extra"
        else (replacement, *expected[1:])
    )

    with pytest.raises(AssertionError, match="entry-point inventory differs"):
        verify_inventory(external, observed)


def test_listing_measurement_retains_rejected_inventory(tmp_path: Path) -> None:
    setup = tmp_path / "setup"
    setup.mkdir()
    wheel = build_fixture_wheel(
        setup, {"policy.unexpected": "kora_tooling_fixture.providers:good"}
    )
    python = install_measurement_fixture(setup, wheel)
    output = tmp_path / "report.json"

    completed = subprocess.run(
        [
            str(python),
            "-c",
            "import sys; from pathlib import Path; "
            "from tests.measurements.listing_measurement import measure_listing; "
            "measure_listing(Path(sys.argv[1]))",
            str(output),
        ],
        cwd=REPOSITORY,
        env={
            **os.environ,
            "KORA_MEASUREMENT_REVISION": "95feb2a650819c735e4f8113193130acab4dd5a9",
        },
        capture_output=True,
        check=False,
        timeout=60,
    )
    evidence = TypeAdapter(JsonObjectField).validate_json(
        (tmp_path / "listing.json").read_bytes()
    )
    datasets = evidence["datasets"]
    assert isinstance(datasets, list)
    record = datasets[0]
    assert isinstance(record, dict)
    retained = Path((tmp_path / "listing-artifacts/10/control-root.txt").read_text())
    retained.rmdir()

    assert completed.returncode != 0
    assert b"entry-point inventory differs" in completed.stderr
    assert evidence["all_420_cycles"] is False
    assert record["inventory_verified"] is False
    assert record["verified_inventory"] == []
    assert record["entries"] == 10
    assert record["fixture_entries"] == 1
    assert record["fixture_schema_bytes"] == 8192
    declared = record["declared_inventory"]
    observed = record["observed_inventory"]
    assert isinstance(declared, list)
    assert isinstance(observed, list)
    assert len(declared) == 10
    assert len(observed) == 2
    observed_entries = TypeAdapter(tuple[InstalledEntry, ...]).validate_python(observed)
    assert sorted(entry.name for entry in observed_entries) == [
        "policy.unexpected",
        "policy.zz_controlled",
    ]
    assert not (tmp_path / "listing-artifacts/10/0").exists()
