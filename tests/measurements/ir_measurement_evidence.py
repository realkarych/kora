import base64
import hashlib
import json
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from xml.parsers.expat import ParserCreate

from pydantic import BaseModel, ConfigDict, StrictBool, TypeAdapter, ValidationError

from tests.measurements.cost_workload import (
    DENSE_MEASURED as COST_DENSE_MEASURED,
)
from tests.measurements.cost_workload import (
    LARGE_CYCLES,
    ORDINARY_CYCLES,
    SUMMARY_BYTES,
    CostCase,
    cost_cases,
)
from tests.measurements.cost_workload import MEASURED as COST_MEASURED
from tests.measurements.cost_workload import WARM_UP as COST_WARM_UP
from tests.measurements.harness import ExtraValue
from tests.measurements.ir_config_lifetime import CONFIGURATIONS
from tests.measurements.ir_declaration_workload import LIFETIME_TRIGGERS
from tests.measurements.ir_document_workload import LIFETIME_DOCUMENT, graph_inventory
from tests.measurements.ir_remaining_workload import OPEN, REFUSED_OPEN
from tests.measurements.test_ir_models_cost import (
    COST_RETAINED_LIMIT_BYTES,
    COST_RSS_LIMIT_BYTES,
    COST_TIMING_LIMIT_SECONDS,
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
ACCOUNTING_OUTPUT_LIMIT: int = 262_144
ACCOUNTING_ENCODED_LIMIT: int = ((ACCOUNTING_OUTPUT_LIMIT + 2) // 3) * 4
ACCOUNTING_ENVELOPE_LIMIT: int = 2 * ACCOUNTING_ENCODED_LIMIT + 8192
ACCOUNTING_CHECKPOINTS: TypeAdapter[list[dict[str, int]]] = TypeAdapter(
    list[dict[str, int]]
)
ACCOUNTING_PATHS: TypeAdapter[list[str]] = TypeAdapter(list[str])
ACCOUNTING_CASES: TypeAdapter[dict[str, list[str]]] = TypeAdapter(dict[str, list[str]])


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


class AccountingProcess(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    revision: str
    run: int
    mode: Literal["timing", "traced", "native"]
    group: Literal["ordinary", "large"]
    batch: int
    cases: list[str]
    pid: int
    code: int
    timed_out: StrictBool
    overflow: StrictBool
    elapsed_seconds: float
    stdout_base64: str
    stderr_base64: str
    output_scope: str


class AccountingEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run: str
    pid: int
    pydantic: str
    pydantic_core: str
    executable: str
    cwd: str
    warm_up: int
    ordinary_measured: int
    large_measured: int
    operations_per_batch: int
    ordinary_cycles: int
    large_cycles: int
    rss_source: str
    peak_rss_source: str
    unowned_resources: str


class AccountingLifetimeEnvironment(AccountingEnvironment):
    traced: StrictBool
    cycles: int
    successes: int
    refusals: int
    retaining_paths: str
    checkpoints: str


@dataclass(frozen=True)
class AccountingChild:
    identity: str
    node: str
    run: int
    mode: Literal["timing", "traced", "native"]
    group: Literal["ordinary", "large"]
    batch: int
    cases: tuple[CostCase, ...]

    def samples(self) -> Counter[tuple[str, int, int]]:
        if self.mode == "timing":
            return Counter(
                (f"cost_{case.name}", repeat, case.records)
                for case in self.cases
                for repeat in range(
                    COST_MEASURED if self.group == "ordinary" else COST_DENSE_MEASURED
                )
            )
        return Counter(
            {(f"cost_{self.group}_{self.mode}_lifetime", 0, self.cycles()): 1}
        )

    def cycles(self) -> int:
        return ORDINARY_CYCLES if self.group == "ordinary" else LARGE_CYCLES

    def parent_sample(self) -> tuple[str, int, int]:
        workload = (
            f"cost_{self.group}_timing_process"
            if self.mode == "timing"
            else f"cost_{self.mode}_{self.group}_lifetime_process"
        )
        return workload, self.run, 1


def accounting_children(actions: list[CostCase] | None = None) -> list[AccountingChild]:
    if actions is None:
        actions = cost_cases()
    children: list[AccountingChild] = []
    groups: tuple[Literal["ordinary", "large"], ...] = ("ordinary", "large")
    modes: tuple[Literal["traced", "native"], ...] = ("traced", "native")
    for run in range(3):
        for group in groups:
            selected = tuple(case for case in actions if case.group == group)
            timing_batches = [selected]
            if group == "large":
                timing_batches = [
                    tuple(
                        case
                        for case in selected
                        if not case.name.startswith("snapshot_bytes")
                    )
                ]
                timing_batches.extend(
                    (case,)
                    for case in selected
                    if case.name.startswith("snapshot_bytes")
                )
            for batch, batch_cases in enumerate(timing_batches):
                children.append(
                    AccountingChild(
                        f"cost_timing_{group}_{batch}_{run}",
                        f"{NODE}test_accounting_value_cost[{group}-{run}]",
                        run,
                        "timing",
                        group,
                        batch,
                        batch_cases,
                    )
                )
            for mode in modes:
                batches = (
                    [(case,) for case in selected]
                    if mode == "traced" and group == "large"
                    else [selected]
                )
                for batch, batch_cases in enumerate(batches):
                    name = (
                        batch_cases[0].name
                        if mode == "traced" and group == "large"
                        else "all"
                    )
                    children.append(
                        AccountingChild(
                            f"cost_{mode}_{group}_{name}_{run}",
                            f"{NODE}test_accounting_value_lifetime"
                            f"[{mode == 'traced'}-{group}-{run}]",
                            run,
                            mode,
                            group,
                            batch,
                            batch_cases,
                        )
                    )
    return children


def accounting_evidence(children: list[AccountingChild] | None = None) -> Evidence:
    selected = accounting_children() if children is None else children
    nodes = {
        f"{NODE}{name}"
        for name in (
            "test_cost_checker_refuses_unexpected_acceptance",
            "test_cost_checker_refuses_unexpected_rejection",
            "test_cost_checker_releases_nested_values",
            "test_cost_process_failure_preserves_lossless_evidence",
        )
        if any(child.mode == "timing" for child in selected)
    }
    gates: set[str] = set()
    samples: Counter[tuple[str, int, int]] = Counter()
    for child in selected:
        nodes.add(child.node)
        gates.add(f"{child.identity}_completed")
        if child.mode == "timing":
            gates.update(
                f"{child.identity}_cost_{case.name}_p95" for case in child.cases
            )
        else:
            resource = "heap" if child.mode == "traced" else "rss"
            cycles = child.cycles()
            gates.update(
                f"{child.identity}_{cycle}_{resource}_retained"
                for cycle in (0, cycles // 3, cycles * 2 // 3, cycles)
            )
            gates.add(f"{child.identity}_owned_resources_released")
        samples.update(child.samples())
        samples[child.parent_sample()] += 1
    return Evidence(frozenset(nodes), frozenset(gates), samples)


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


def accepted_evidence() -> Evidence:
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


def expected_evidence() -> Evidence:
    accepted = accepted_evidence()
    accounting = accounting_evidence()
    return Evidence(
        accepted.nodes | accounting.nodes,
        accepted.gates | accounting.gates,
        accepted.samples + accounting.samples,
    )


def shard_evidence() -> tuple[Evidence, Evidence, Evidence]:
    accepted = accepted_evidence()
    worker1 = configuration_evidence(1)
    worker2 = configuration_evidence(2)
    children = accounting_children()
    timing = accounting_evidence(
        [child for child in children if child.mode == "timing"]
    )
    lifetime = accounting_evidence(
        [child for child in children if child.mode != "timing"]
    )
    return (
        Evidence(
            accepted.nodes - worker1.nodes - worker2.nodes,
            accepted.gates - worker1.gates - worker2.gates,
            Counter(
                {
                    key: count
                    for key, count in accepted.samples.items()
                    if key not in worker1.samples and key not in worker2.samples
                }
            ),
        ),
        Evidence(
            worker1.nodes | timing.nodes,
            worker1.gates | timing.gates,
            worker1.samples + timing.samples,
        ),
        Evidence(
            worker2.nodes | lifetime.nodes,
            worker2.gates | lifetime.gates,
            worker2.samples + lifetime.samples,
        ),
    )


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


def accounting_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("incomplete IR accounting evidence")
    return result


def accounting_json(raw: object, limit: int) -> object:
    if not isinstance(raw, str) or len(raw) > limit or len(raw.encode()) > limit:
        raise ValueError("incomplete IR accounting evidence")
    return cast(object, json.loads(raw, object_pairs_hook=accounting_object))


def accounting_stream(encoded: str) -> bytes:
    if len(encoded) > ACCOUNTING_ENCODED_LIMIT or not encoded.isascii():
        raise ValueError("incomplete IR accounting evidence")
    decoded = base64.b64decode(encoded, validate=True)
    if (
        len(decoded) > ACCOUNTING_OUTPUT_LIMIT
        or base64.b64encode(decoded).decode("ascii") != encoded
    ):
        raise ValueError("incomplete IR accounting evidence")
    return decoded


def accounting_sample_values(
    samples: list[MeasurementSample],
) -> Counter[tuple[str, int, int, float, float]]:
    return Counter(
        (
            sample.workload,
            sample.repeat,
            sample.count,
            sample.seconds,
            sample.us_per_record,
        )
        for sample in samples
    )


def verify_accounting_lifetime(
    environment: AccountingLifetimeEnvironment, child: AccountingChild
) -> None:
    points = ACCOUNTING_CHECKPOINTS.validate_python(
        accounting_json(environment.checkpoints, ACCOUNTING_OUTPUT_LIMIT), strict=True
    )
    paths = ACCOUNTING_PATHS.validate_python(
        accounting_json(environment.retaining_paths, ACCOUNTING_OUTPUT_LIMIT),
        strict=True,
    )
    cycles = child.cycles()
    traced = child.mode == "traced"
    if (
        environment.traced is not traced
        or environment.cycles != cycles
        or environment.successes != cycles * sum(case.accepted for case in child.cases)
        or environment.refusals
        != cycles * sum(not case.accepted for case in child.cases)
        or len(points) != 4
        or len(paths) != 3
        or any(len(path.encode()) > SUMMARY_BYTES for path in paths)
        or not traced
        and any(paths)
    ):
        raise ValueError("inconsistent IR accounting lifetime evidence")
    fields = {
        "cycle",
        "heap",
        "peak",
        "tracer",
        "rss",
        "high_water",
        "descriptors",
        "threads",
    }
    if any(set(point) != fields for point in points) or [
        point["cycle"] for point in points
    ] != [0, cycles // 3, cycles * 2 // 3, cycles]:
        raise ValueError("inconsistent IR accounting lifetime evidence")
    for point in points:
        if (
            any(
                point[name] < 0 or point[name] != points[0][name]
                for name in ("descriptors", "threads")
            )
            or any(
                point[name] < 0 if traced else point[name] != -1
                for name in ("heap", "peak", "tracer")
            )
            or any(
                point[name] != -1 if traced else point[name] < 0
                for name in ("rss", "high_water")
            )
            or traced
            and point["heap"] - points[0]["heap"] > COST_RETAINED_LIMIT_BYTES
            or not traced
            and point["rss"] - points[0]["rss"] > COST_RSS_LIMIT_BYTES
        ):
            raise ValueError("inconsistent IR accounting lifetime evidence")


def verify_accounting_child(
    report: MeasurementReport, child: AccountingChild
) -> Counter[tuple[str, int, int, float, float]]:
    process = AccountingProcess.model_validate(
        accounting_json(
            report.environment.get(f"{child.identity}_process"),
            ACCOUNTING_ENVELOPE_LIMIT,
        )
    )
    if (
        process.revision != report.revision
        or (process.run, process.mode, process.group, process.batch)
        != (child.run, child.mode, child.group, child.batch)
        or process.cases != [case.name for case in child.cases]
        or process.pid <= 0
        or process.code != 0
        or process.timed_out
        or process.overflow
        or process.elapsed_seconds < 0
        or process.output_scope != "all_runner_retained_bytes_capped_per_stream"
    ):
        raise ValueError("inconsistent IR accounting process evidence")
    raw = report.environment.get(child.identity)
    if not isinstance(raw, str) or len(raw) > ACCOUNTING_OUTPUT_LIMIT:
        raise ValueError("incomplete IR accounting child evidence")
    stdout = accounting_stream(process.stdout_base64)
    stderr = accounting_stream(process.stderr_base64)
    if stderr or stdout.decode() != raw:
        raise ValueError("inconsistent IR accounting child evidence")
    payload = MeasurementReport.model_validate(
        accounting_json(raw, ACCOUNTING_OUTPUT_LIMIT)
    )
    environment = (
        AccountingEnvironment.model_validate(payload.environment)
        if child.mode == "timing"
        else AccountingLifetimeEnvironment.model_validate(payload.environment)
    )
    if (
        (payload.revision, payload.platform, payload.python)
        != (report.revision, report.platform, report.python)
        or environment.run != child.identity
        or environment.pid != process.pid
        or not all(
            (
                environment.pydantic,
                environment.pydantic_core,
                environment.executable,
                environment.cwd,
            )
        )
        or environment.warm_up != COST_WARM_UP
        or environment.ordinary_measured != COST_MEASURED
        or environment.large_measured != COST_DENSE_MEASURED
        or environment.operations_per_batch != 1
        or environment.ordinary_cycles != ORDINARY_CYCLES
        or environment.large_cycles != LARGE_CYCLES
        or environment.rss_source
        != (
            "/bin/ps rss KiB converted to bytes"
            if report.platform == "darwin"
            else "/proc/self/statm resident pages converted to bytes"
        )
        or environment.peak_rss_source
        != "getrusage RUSAGE_SELF ru_maxrss converted to bytes"
        or environment.unowned_resources
        != "no tasks, connections, timers or subscriptions acquired"
        or payload.gates
        or payload.durations_seconds
        or Counter(
            (sample.workload, sample.repeat, sample.count) for sample in payload.samples
        )
        != child.samples()
    ):
        raise ValueError("inconsistent IR accounting child evidence")
    grouped: dict[str, list[float]] = {}
    for sample in payload.samples:
        if (
            sample.seconds < 0
            or sample.us_per_record != sample.seconds / sample.count * 1_000_000
        ):
            raise ValueError("inconsistent IR accounting sample evidence")
        grouped.setdefault(sample.workload, []).append(sample.us_per_record)
    summaries = {
        name: {
            "median_us_per_record": statistics.median(values),
            "p95_us_per_record": sorted(values)[max(0, int(len(values) * 0.95) - 1)],
            "repeats": len(values),
        }
        for name, values in grouped.items()
    }
    if payload.summary != summaries:
        raise ValueError("inconsistent IR accounting sample evidence")
    if isinstance(environment, AccountingLifetimeEnvironment):
        verify_accounting_lifetime(environment, child)
    elif any(
        summaries[f"cost_{case.name}"]["p95_us_per_record"] * case.records / 1_000_000
        > COST_TIMING_LIMIT_SECONDS[f"cost_{case.name}"]
        for case in child.cases
    ):
        raise ValueError("inconsistent IR accounting timing evidence")
    samples = accounting_sample_values(payload.samples)
    name, repeat, count = child.parent_sample()
    samples[
        (
            name,
            repeat,
            count,
            process.elapsed_seconds,
            process.elapsed_seconds * 1_000_000,
        )
    ] += 1
    return samples


def verify_accounting_evidence(report: MeasurementReport, expected: Evidence) -> None:
    actions = cost_cases()
    selected = [
        child for child in accounting_children(actions) if child.node in expected.nodes
    ]
    identities = {child.identity for child in selected}
    required = identities | {f"{identity}_process" for identity in identities}
    if {name for name in report.environment if name.startswith("cost_")} != required:
        raise ValueError("incomplete IR accounting child evidence")
    try:
        if selected:
            inventory: dict[str, list[str]] = {"ordinary": [], "large": []}
            for case in actions:
                inventory[case.group].append(case.name)
            recorded = ACCOUNTING_CASES.validate_python(
                accounting_json(
                    report.environment.get("accounting_case_inventory"),
                    ACCOUNTING_OUTPUT_LIMIT,
                ),
                strict=True,
            )
            if recorded != inventory:
                raise ValueError("inconsistent IR accounting case inventory")
        samples: Counter[tuple[str, int, int, float, float]] = Counter()
        for child in selected:
            samples.update(verify_accounting_child(report, child))
        if samples != accounting_sample_values(
            [sample for sample in report.samples if sample.workload.startswith("cost_")]
        ):
            raise ValueError("inconsistent IR accounting sample evidence")
    except (ValueError, RecursionError):
        raise ValueError("incomplete IR accounting evidence") from None


def verify_report(
    document: str, junit: str, revision: str, expected: Evidence
) -> MeasurementReport:
    try:
        report = MeasurementReport.model_validate_json(document)
    except ValidationError:
        raise ValueError("incomplete IR shard evidence") from None
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
    verify_accounting_evidence(report, expected)
    repeats = Counter(sample.workload for sample in report.samples)
    if set(report.summary) != set(repeats) or any(
        report.summary[name].get("repeats") != count for name, count in repeats.items()
    ):
        raise ValueError("incomplete IR sample summary")
    return report


def verify_union(directory: Path, revision: str) -> dict[str, object]:
    full = expected_evidence()
    expected = shard_evidence()
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
        or (len(selected), len(gates), sum(samples.values())) != (352, 1211, 10536)
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
