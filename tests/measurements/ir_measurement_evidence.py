import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from xml.parsers.expat import ParserCreate

from pydantic import BaseModel, ConfigDict, StrictBool, TypeAdapter, ValidationError

from tests.measurements.harness import ExtraValue
from tests.measurements.ir_config_lifetime import CONFIGURATIONS
from tests.measurements.ir_declaration_workload import LIFETIME_TRIGGERS
from tests.measurements.ir_document_workload import LIFETIME_DOCUMENT, graph_inventory
from tests.measurements.ir_remaining_workload import OPEN, REFUSED_OPEN
from tests.measurements.test_ir_models_cost import (
    DENSE_MEASURED,
    FAILURE_CYCLES,
    FAILURES,
    MEASURED,
    RETAINED_LIMIT_BYTES,
    WARM_UP,
    WORKLOADS,
    cases,
)
from tests.measurements.vcs_ref_workload import BATCHES, INTERVAL
from tests.measurements.vcs_ref_workload import cases as vcs_ref_cases

ROOT = Path(__file__).resolve().parents[2]
MODULE = "tests/measurements/test_ir_models_cost.py"
NODE = f"{MODULE}::"
SELECTED_NODES = TypeAdapter(list[str])
VCS_REF_MANIFEST = TypeAdapter(dict[str, dict[str, int]])


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


class AllocationDifference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    location: str
    bytes: int
    count: int


class ConfigurationCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    cycles: int
    seconds: float
    retained_bytes: int
    peak_traced_bytes: int
    current_rss_bytes: int | None
    peak_rss_bytes: int | None
    live_models: int
    threads: int
    os_threads: int | None
    tracer_bytes: int
    descriptors: int
    allocation_differences: list[AllocationDifference]


class ConfigurationLifetime(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    baseline_rss_bytes: int
    baseline_peak_rss_bytes: int
    baseline_threads: int
    baseline_os_threads: int | None
    baseline_descriptors: int
    checkpoints: list[ConfigurationCheckpoint]
    cycles: int
    models_per_cycle: int
    class_schema_inventory: list[str]
    class_schemas_unchanged: StrictBool
    supplemental_fixtures: int
    trigger_union_array_size: int
    trigger_union_schema_unchanged: StrictBool
    document_miniature_nodes: int
    document_full_size_slots: int
    document_full_size_rotation: dict[str, int]
    warm_up: int
    rss_source: str


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
    nodes.add(f"{NODE}test_vcs_ref_action_cost")
    for action in vcs_ref_cases()[0]:
        gates.add(f"{action.name}_retained")
        samples.update((action.name, repeat, 1) for repeat in range(DENSE_MEASURED))
    for run in range(3):
        nodes.add(f"{NODE}test_vcs_ref_cold_public_import[{run}]")
        gates.add(f"vcs_import_{run}_resources_released")
        for source in ("base", "head"):
            samples[(f"vcs_import_{source}", run, 1)] += 1
        for traced in (True, False):
            mode = "traced" if traced else "untraced"
            nodes.add(f"{NODE}test_vcs_ref_validation_lifetime[{run}-{traced}]")
            gates.add(f"vcs_ref_{mode}_{run}_lifecycle_assertions_passed")
            samples[(f"vcs_ref_{mode}_{run}_process", run, BATCHES * INTERVAL)] += 1
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


def verify_configuration_payloads(
    report: MeasurementReport, expected: Evidence
) -> None:
    runs = [
        run for run in range(3) if configuration_evidence(run).nodes <= expected.nodes
    ]
    if {name for name in report.environment if name.startswith("config_lifetime_")} != {
        f"config_lifetime_{run}" for run in runs
    }:
        raise ValueError("incomplete IR configuration lifetime evidence")
    for run in runs:
        raw = report.environment[f"config_lifetime_{run}"]
        if not isinstance(raw, str):
            raise ValueError("incomplete IR configuration lifetime evidence")
        try:
            payload = ConfigurationLifetime.model_validate_json(raw)
        except ValidationError:
            raise ValueError("incomplete IR configuration lifetime evidence") from None
        cycles = sorted(count for _, _, count in configuration_evidence(run).samples)
        if (
            payload.cycles != cycles[-1]
            or payload.models_per_cycle != len(CONFIGURATIONS)
            or payload.class_schema_inventory
            != sorted(model.__name__ for model in CONFIGURATIONS)
            or not payload.class_schemas_unchanged
            or not payload.trigger_union_schema_unchanged
            or payload.supplemental_fixtures != len(OPEN) + len(REFUSED_OPEN)
            or payload.trigger_union_array_size != len(LIFETIME_TRIGGERS)
            or payload.document_miniature_nodes
            != graph_inventory(LIFETIME_DOCUMENT)["nodes"]
            or payload.document_full_size_slots != payload.cycles // 100
            or payload.document_full_size_rotation
            != {
                "flat_valid": 3,
                "flat_final_invalid": 3,
                "flat_all_invalid": 2,
                "nested_valid": 2,
            }
            or payload.warm_up != WARM_UP
            or [point.cycles for point in payload.checkpoints] != cycles
            or report.platform == "linux"
            and payload.baseline_os_threads is None
        ):
            raise ValueError("inconsistent IR configuration lifetime inventory")
        times = {
            sample.count: sample.seconds
            for sample in report.samples
            if sample.workload == "config_lifetime" and sample.repeat == run
        }
        for point in payload.checkpoints:
            if (
                point.seconds != times.get(point.cycles)
                or point.retained_bytes >= RETAINED_LIMIT_BYTES
                or point.live_models != 0
                or point.threads != payload.baseline_threads
                or point.descriptors != payload.baseline_descriptors
                or point.os_threads != payload.baseline_os_threads
                or report.platform == "linux"
                and (
                    point.current_rss_bytes is None
                    or point.peak_rss_bytes is None
                    or point.os_threads is None
                )
            ):
                raise ValueError("inconsistent IR configuration lifetime checkpoint")


def verify_vcs_ref_evidence(report: MeasurementReport, expected: Evidence) -> None:
    manifest = report.environment.get("vcs_ref_fixture_manifest")
    if f"{NODE}test_vcs_ref_action_cost" in expected.nodes:
        if not isinstance(manifest, str):
            raise ValueError("incomplete IR VcsRef fixture manifest")
        try:
            recorded = VCS_REF_MANIFEST.validate_json(manifest, strict=True)
        except ValidationError:
            raise ValueError("incomplete IR VcsRef fixture manifest") from None
        if recorded != vcs_ref_cases()[1]:
            raise ValueError("inconsistent IR VcsRef fixture manifest")
    elif "vcs_ref_fixture_manifest" in report.environment:
        raise ValueError("unselected IR VcsRef fixture manifest")
    for run in range(3):
        if f"{NODE}test_vcs_ref_cold_public_import[{run}]" in expected.nodes:
            for source in ("base", "head"):
                output = report.environment.get(f"vcs_import_{source}_{run}")
                if not isinstance(output, str) or not output.strip():
                    raise ValueError("incomplete IR VcsRef import evidence")
        for traced in (True, False):
            if (
                f"{NODE}test_vcs_ref_validation_lifetime[{run}-{traced}]"
                not in expected.nodes
            ):
                continue
            mode = "traced" if traced else "untraced"
            prefix = f"vcs_ref_{mode}_{run}"
            output = report.environment.get(f"{prefix}_stdout")
            code = report.environment.get(f"{prefix}_exit")
            if (
                not isinstance(output, str)
                or not output.strip()
                or report.environment.get(f"{prefix}_stderr") != ""
                or type(code) is not int
                or code != 0
            ):
                raise ValueError("incomplete IR VcsRef lifetime evidence")


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
    verify_configuration_payloads(report, expected)
    verify_vcs_ref_evidence(report, expected)
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
        or (len(selected), len(gates), sum(samples.values())) != (330, 446, 7791)
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
