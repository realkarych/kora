import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from tests.measurements.harness import Report, Sample, report_lifecycle
from tests.support.observability import run_python
from tests.support.provider_distributions import Distribution
from tests.support.provider_distributions import (
    write_distribution as write_distribution_metadata,
)

pytestmark = pytest.mark.measurement

PROBE: Path = Path(__file__).with_name("provider_loading_probe.py")
INDEPENDENT_RUNS: int = 3
MEASURED: int = 50
HEAP_GROWTH_LIMIT_BYTES: int = 262_144
WORKLOADS: tuple[str, ...] = (
    "load_provider",
    "not_installed",
    "raising_factory",
    "discover_providers",
)
RAISING_MODULE: str = 'def provider() -> object:\n    raise RuntimeError("cost")\n'
PREFIX: str = "kora_loading_cost_"
SINGLE_ENTRY_DISTRIBUTIONS: int = 50
ENTRIES_IN_ONE_DISTRIBUTION: int = 50
PROVIDER_MODULE: str = "\n".join(
    [
        "from kora.protocols.metadata import ProtocolName, ProviderMetadata",
        "",
        "",
        "def implementation(configuration: object) -> object:",
        "    return configuration",
        "",
        "",
        "def provider() -> ProviderMetadata:",
        "    return ProviderMetadata(",
        '        ProtocolName.RUNTIME, "{name}", implementation,',
        '        {{"type": "object"}}, "cost-1"',
        "    )",
        "",
    ]
)


@pytest.fixture
def report() -> Iterator[Report]:
    yield from report_lifecycle()


def write_distribution(site: Path, name: str, entries: list[str]) -> None:
    distribution = Distribution(
        name,
        "1.0",
        {f"runtime.{entry}": f"{PREFIX}{entry}:provider" for entry in entries},
    )
    write_distribution_metadata(site, distribution)
    for entry in entries:
        module = site / f"{PREFIX}{entry}.py"
        module.write_text(PROVIDER_MODULE.format(name=entry))


def build_site(root: Path) -> Path:
    site = root / "site"
    site.mkdir()
    for number in range(SINGLE_ENTRY_DISTRIBUTIONS):
        write_distribution(
            site, f"kora-loading-cost-{number:02d}", [f"cost_{number:02d}"]
        )
    many = [f"many_{number:02d}" for number in range(ENTRIES_IN_ONE_DISTRIBUTION)]
    write_distribution(site, "kora-loading-cost-many", many)
    write_distribution(site, "kora-loading-cost-raising", ["raising"])
    (site / f"{PREFIX}raising.py").write_text(RAISING_MODULE)
    return site


def run_child(site: Path, log: Path) -> dict[str, object]:
    completed = run_python(["-I", str(PROBE), str(site), str(log)], {})
    assert completed.code == 0, completed.stderr
    assert completed.stderr == b""
    result: dict[str, object] = json.loads(completed.stdout)
    return result


def record(report: Report, run: int, result: dict[str, object]) -> None:
    report.samples.append(
        Sample(
            "cold_load_provider", run, 1, cast("float", result["cold_load_provider"])
        )
    )
    for workload in WORKLOADS:
        timings = cast("list[float]", result[workload])
        assert len(timings) == MEASURED
        for repeat, seconds in enumerate(timings):
            report.samples.append(Sample(workload, run * MEASURED + repeat, 1, seconds))
    before = cast("dict[str, int]", result["before"])
    after = cast("dict[str, int]", result["after"])
    for key, value in before.items():
        report.environment[f"run_{run}_{key}_before"] = value
        report.environment[f"run_{run}_{key}_after"] = after[key]


def test_provider_loading_cost(tmp_path: Path, report: Report) -> None:
    site = build_site(tmp_path)

    results = [
        run_child(site, tmp_path / f"diagnostics-{run}.jsonl")
        for run in range(INDEPENDENT_RUNS)
    ]

    for run, result in enumerate(results):
        record(report, run, result)
        before = cast("dict[str, int]", result["before"])
        after = cast("dict[str, int]", result["after"])
        assert after["modules"] == before["modules"]
        assert after["descriptors"] == before["descriptors"]
        report.diagnostic(
            f"provider_loading_{run}_heap_bytes",
            after["heap_bytes"] - before["heap_bytes"],
            HEAP_GROWTH_LIMIT_BYTES,
            allow_equal=False,
        )
        assert cast("int", result["providers"]) >= (
            SINGLE_ENTRY_DISTRIBUTIONS + ENTRIES_IN_ONE_DISTRIBUTION
        )
        assert result["drained"] is True
    report.environment["installed_distributions"] = cast(
        "int", results[0]["installed_distributions"]
    )
    report.environment["generated_distributions"] = SINGLE_ENTRY_DISTRIBUTIONS + 2
    report.environment["independent_runs"] = INDEPENDENT_RUNS
