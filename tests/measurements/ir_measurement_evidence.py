import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from xml.parsers.expat import ParserCreate

from pydantic import BaseModel, ConfigDict, StrictBool, TypeAdapter

from tests.measurements.harness import ExtraValue
from tests.measurements.test_ir_models_cost import (
    FAILURE_CYCLES,
    FAILURES,
    MEASURED,
    WORKLOADS,
    cases,
)

ROOT = Path(__file__).resolve().parents[2]
MODULE = "tests/measurements/test_ir_models_cost.py"
NODE = f"{MODULE}::"
SELECTED_NODES = TypeAdapter(list[str])


@dataclass(frozen=True)
class Evidence:
    nodes: frozenset[str]
    gates: frozenset[str]
    samples: Counter[tuple[str, int, int]]


class MeasurementSample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    workload: str
    repeat: int
    count: int
    seconds: float
    us_per_record: float


class MeasurementReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    started_at: str
    platform: str
    python: str
    revision: str
    environment: dict[str, ExtraValue]
    summary: dict[str, dict[str, float]]
    gates: dict[str, StrictBool]
    durations_seconds: dict[str, float]
    samples: list[MeasurementSample]


def configuration_evidence(run: int) -> Evidence:
    return Evidence(
        frozenset({f"{NODE}test_ir_configuration_repeated_lifetime[{run}]"}),
        frozenset(
            f"config_lifetime_{run}_{cycle}_retained" for cycle in (250, 500, 750, 1000)
        ),
        Counter(("config_lifetime", run, cycle) for cycle in (250, 500, 750, 1000)),
    )


def expected_evidence() -> Evidence:
    nodes = {f"{NODE}test_ir_model_validation_cost[{name}]" for name in WORKLOADS} | {
        f"{NODE}test_repeated_failures_return_to_baseline",
        f"{NODE}test_shared_diagnostic_action_cost",
    }
    gates = {f"{name}_retained_at_baseline" for name in WORKLOADS} | {
        "failure_cycles_return_to_baseline"
    }
    samples = Counter(
        (name, repeat, count)
        for name, (_, count, _, repeats) in WORKLOADS.items()
        for repeat in range(repeats)
    )
    samples[("failure_cycles", 0, FAILURE_CYCLES * len(FAILURES))] += 1
    for action in cases()[0]:
        samples.update(
            (f"diagnostic_{action.name}", repeat, 1) for repeat in range(MEASURED)
        )
    for run in range(3):
        worker = configuration_evidence(run)
        nodes.update(worker.nodes)
        gates.update(worker.gates)
        samples.update(worker.samples)
        for traced in (True, False):
            mode = "traced" if traced else "untraced"
            nodes.add(f"{NODE}test_shared_diagnostic_resource_lifetime[{run}-{traced}]")
            gates.add(f"diagnostic_{mode}_{run}_resources_released")
            samples[(f"diagnostic_{mode}_{run}_process", run, 1)] += 1
    return Evidence(frozenset(nodes), frozenset(gates), samples)


def junit_nodes(document: str) -> frozenset[str]:
    nodes: set[str] = set()

    def start(name: str, attributes: dict[str, str]) -> None:
        if name in {"failure", "error", "skipped"}:
            raise ValueError("incomplete IR test evidence")
        if name != "testcase":
            return
        classname = attributes.get("classname")
        test = attributes.get("name")
        if classname != "tests.measurements.test_ir_models_cost" or not test:
            raise ValueError("incomplete IR test evidence")
        identity = f"{NODE}{test}"
        if identity in nodes:
            raise ValueError("incomplete IR test evidence")
        nodes.add(identity)

    def refuse_doctype(
        name: str, system_id: str | None, public_id: str | None, subset: int
    ) -> None:
        raise ValueError("incomplete IR test evidence")

    parser = ParserCreate()
    parser.StartElementHandler = start
    parser.StartDoctypeDeclHandler = refuse_doctype
    parser.Parse(document, True)
    return frozenset(nodes)


def verify_report(
    document: str, junit: str, revision: str, expected: Evidence
) -> MeasurementReport:
    report = MeasurementReport.model_validate_json(document)
    selected = report.environment.get("selected_test_nodes")
    if not isinstance(selected, str):
        raise ValueError("incomplete IR test inventory")
    selected_nodes = SELECTED_NODES.validate_json(selected, strict=True)
    sample_identities = Counter(
        (sample.workload, sample.repeat, sample.count) for sample in report.samples
    )
    if (
        not revision
        or report.revision != revision
        or report.platform != sys.platform
        or report.python != sys.version.split()[0]
        or report.environment.get("uv_lock_sha256")
        != hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
        or len(selected_nodes) != len(set(selected_nodes))
        or frozenset(selected_nodes) != expected.nodes
        or junit_nodes(junit) != expected.nodes
        or frozenset(report.gates) != expected.gates
        or not all(report.gates.values())
        or sample_identities != expected.samples
    ):
        raise ValueError("incomplete IR shard evidence")
    repeats = Counter(sample.workload for sample in report.samples)
    if set(report.summary) != set(repeats) or any(
        report.summary[name].get("repeats") != count for name, count in repeats.items()
    ):
        raise ValueError("incomplete IR sample summary")
    return report


def verify_union(directory: Path, revision: str) -> dict[str, object]:
    full = expected_evidence()
    worker1 = configuration_evidence(1)
    worker2 = configuration_evidence(2)
    expected = (
        Evidence(
            full.nodes - worker1.nodes - worker2.nodes,
            full.gates - worker1.gates - worker2.gates,
            full.samples - worker1.samples - worker2.samples,
        ),
        worker1,
        worker2,
    )
    required_directories = {
        f"ir-models-cost-measurement-{revision}-{run}" for run in range(3)
    }
    if (
        not directory.is_dir()
        or {path.name for path in directory.iterdir()} != required_directories
    ):
        raise ValueError("incomplete IR shard evidence")
    manifests: dict[str, ExtraValue] | None = None
    selected: set[str] = set()
    gates: set[str] = set()
    samples: Counter[tuple[str, int, int]] = Counter()
    for run, inventory in enumerate(expected):
        shard = directory / f"ir-models-cost-measurement-{revision}-{run}"
        required_files = {
            f"{prefix}-{run}.{suffix}"
            for prefix, suffix in (
                ("ir-models-cost", "json"),
                ("ir-models-cost", "xml"),
                ("ir-checkout-revision", "txt"),
            )
        }
        if (
            not shard.is_dir()
            or {path.name for path in shard.iterdir()} != required_files
        ):
            raise ValueError("incomplete IR shard evidence")
        if (shard / f"ir-checkout-revision-{run}.txt").read_text().strip() != revision:
            raise ValueError("IR checkout revision mismatch")
        report = verify_report(
            (shard / f"ir-models-cost-{run}.json").read_text(),
            (shard / f"ir-models-cost-{run}.xml").read_text(),
            revision,
            inventory,
        )
        actual_manifests = {
            name: report.environment.get(name)
            for name in (
                "remaining_fixture_manifest",
                "declaration_fixture_manifest",
                "document_fixture_manifest",
            )
        }
        if any(not value for value in actual_manifests.values()):
            raise ValueError("missing IR fixture identity")
        if manifests is not None and actual_manifests != manifests:
            raise ValueError("IR fixture identity mismatch")
        manifests = actual_manifests
        if selected & inventory.nodes or gates & inventory.gates:
            raise ValueError("duplicate IR shard evidence")
        selected.update(inventory.nodes)
        gates.update(report.gates)
        samples.update(
            (sample.workload, sample.repeat, sample.count) for sample in report.samples
        )
    if (
        frozenset(selected) != full.nodes
        or frozenset(gates) != full.gates
        or samples != full.samples
        or (len(selected), len(gates), sum(samples.values())) != (320, 328, 6689)
    ):
        raise ValueError("incomplete IR evidence union")
    return {
        "scope": "complete IR shard union",
        "revision": revision,
        "tests": len(selected),
        "gates": len(gates),
        "samples": sum(samples.values()),
        "selected_test_nodes": sorted(selected),
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise ValueError("expected IR shard directory and exact revision")
    sys.stdout.write(
        json.dumps(verify_union(Path(sys.argv[1]), sys.argv[2]), indent=2) + "\n"
    )
