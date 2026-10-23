import gc
import json
import subprocess
import sys
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

from kora.ir import CommandConfig, CommandResource, McpServer, NodeLimits, ToolPolicy
from kora.ir.cost import PriceRevision, parse_money
from kora.ir.json import (
    DEFAULT_MAX_DEPTH,
    MAX_CANONICAL_BYTES,
    JsonObject,
    JsonShapeError,
    JsonValue,
    parse_json_object,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.metadata import parse_config_schema
from tests.ir.config_cases import MINIMAL, POPULATED, RESOURCE
from tests.ir.test_cost_models import rates_payload, revision_payload
from tests.measurements.cel_measurement_process import run_process
from tests.measurements.cost_workload import CostCase, cost_cases, exercise_cost
from tests.measurements.diagnostic_workload import Lifecycle, cases, exercise
from tests.measurements.harness import (
    Report,
    Sample,
    open_descriptors,
    report_lifecycle,
)
from tests.measurements.ir_config_workload import (
    export_action,
    round_trip_action,
    validation_action,
)

pytestmark = pytest.mark.measurement

WARM_UP: int = 3
MEASURED: int = 30
DENSE_MEASURED: int = 10
PATTERNS: int = 250_000
FAILURE_CYCLES: int = 1_000
RETAINED_LIMIT_BYTES: int = 65_536
LEAF_BYTES: int = 1024
CONFIG_SCHEMA_BYTES: int = 65_536

COST_RETAINED_LIMIT_BYTES: int = 262_144
COST_RSS_LIMIT_BYTES: int = 8_388_608
COST_TIMING_LIMIT_SECONDS: dict[str, float] = {
    "cost_union_1": 2.0**-16,
    "cost_union_16": 2.0**-14,
    "cost_union_64": 2.0**-10,
    "cost_union_65_refused": 2.0**-10,
    "cost_coverage_complete": 2.0**-13,
    "cost_coverage_initial": 2.0**-14,
    "cost_coverage_gap_refused": 2.0**-13,
    "cost_coverage_sequence_refused": 2.0**-14,
    "cost_observation_valid": 2.0**-12,
    "cost_observation_tokens_refused": 2.0**-13,
    "cost_observation_ttl_refused": 2.0**-12,
    "cost_observation_reasoning_refused": 2.0**-12,
    "cost_snapshot_1_validate": 2.0**-10,
    "cost_snapshot_1_round_trip": 2.0**-10,
    "cost_pricing_max_products": 2.0**-14,
    "cost_pricing_overflow_refused": 2.0**-16,
    "cost_pricing_unknown": 2.0**-18,
    "cost_pricing_subscription": 2.0**-19,
    "cost_snapshot_64_validate": 2.0**-6,
    "cost_snapshot_64_round_trip": 2.0**-5,
    "cost_snapshot_1024_validate": 2.0**-2,
    "cost_snapshot_1024_round_trip": 2.0**-1,
    "cost_revision_65535_validate": 2.0**-5,
    "cost_revision_65535_parse": 2.0**-6,
    "cost_revision_65536_validate": 2.0**-6,
    "cost_revision_65536_parse": 2.0**-6,
    "cost_revision_65537_validate": 2.0**-8,
    "cost_revision_65537_parse": 2.0**-15,
    "cost_revision_65536_round_trip": 2.0**-4,
    "cost_revision_normalized_growth_refused": 2.0**-6,
    "cost_source_8191": 2.0**-11,
    "cost_source_8192": 2.0**-11,
    "cost_source_8193": 2.0**-13,
    "cost_source_8192_round_trip": 2.0**-11,
    "cost_source_8192_dense_round_trip": 2.0**-6,
    "cost_source_depth_refused": 2.0**-13,
    "cost_source_cycle_refused": 2.0**-13,
    "cost_snapshot_subtotal_refused": 2.0**-2,
    "cost_snapshot_1025_refused": 2.0**-4,
    "cost_lexical_money_refused": 2.0**-17,
    "cost_lexical_identifier_refused": 2.0**-9,
    "cost_snapshot_bytes_1048575_validate": 2.0**-2,
    "cost_snapshot_bytes_1048575_parse": 2.0**-2,
    "cost_snapshot_bytes_1048576_validate": 2.0**-2,
    "cost_snapshot_bytes_1048576_parse": 2.0**-2,
    "cost_snapshot_bytes_1048577_validate": 2.0**-4,
    "cost_snapshot_bytes_1048577_parse": 2.0**-9,
    "cost_snapshot_bytes_1048576_round_trip": 2.0**-1,
}


def test_cost_checker_refuses_unexpected_acceptance() -> None:
    case = CostCase("unexpected_acceptance", lambda: object(), False)

    with pytest.raises(AssertionError):
        exercise_cost(case)


def test_cost_checker_refuses_unexpected_rejection() -> None:
    def reject() -> object:
        return parse_money(True)

    case = CostCase("unexpected_rejection", reject)

    with pytest.raises(AssertionError):
        exercise_cost(case)


def test_cost_checker_releases_nested_values() -> None:
    document = revision_payload({"model": rates_payload()})
    case = CostCase("nested_values", lambda: PriceRevision.model_validate(document))

    live_models = exercise_cost(case)

    assert live_models == 0


def append_cost_report(report: Report, output: bytes) -> dict[str, object]:
    payload = cast("dict[str, object]", json.loads(output))
    samples = cast("list[dict[str, object]]", payload["samples"])
    for sample in samples:
        report.samples.append(
            Sample(
                cast("str", sample["workload"]),
                cast("int", sample["repeat"]),
                cast("int", sample["count"]),
                cast("float", sample["seconds"]),
            )
        )
    return payload


@pytest.mark.parametrize("run", range(3))
@pytest.mark.parametrize("group", ["ordinary", "large"])
def test_accounting_value_cost(report: Report, run: int, group: str) -> None:
    batches = [[case.name for case in cost_cases() if case.group == group]]
    if group == "large":
        batches = [
            [
                name
                for name in batches[0]
                if name.startswith("snapshot_bytes") is boundary
            ]
            for boundary in (False, True)
        ]
    before = (open_descriptors(), threading.active_count())

    for batch_index, names in enumerate(batches):
        identity = f"cost_timing_{group}_{batch_index}_{run}"
        argv = [
            sys.executable,
            "-m",
            "tests.measurements.cost_workload",
            "timing",
            group,
            report.revision,
            identity,
            ",".join(names),
        ]
        started = time.perf_counter()
        result = run_process(argv)
        elapsed = time.perf_counter() - started
        assert not result.timed_out
        assert not result.overflow
        assert result.code == 0, result.stderr.decode()
        assert result.stderr == b""
        report.environment[identity] = result.stdout.decode()
        payload = append_cost_report(report, result.stdout)
        environment = cast("dict[str, object]", payload["environment"])
        assert environment["operations_per_batch"] == 1
        summaries = cast("dict[str, dict[str, float]]", payload["summary"])
        counts = {sample.workload: sample.count for sample in report.samples}
        for name, summary in summaries.items():
            seconds = summary["p95_us_per_record"] * counts[name] / 1_000_000
            passed = seconds <= COST_TIMING_LIMIT_SECONDS[name]
            report.gates[f"{identity}_{name}_p95"] = passed
            assert passed, (name, seconds, COST_TIMING_LIMIT_SECONDS[name])
        report.samples.append(Sample(f"cost_{group}_timing_process", run, 1, elapsed))

    assert (open_descriptors(), threading.active_count()) == before


@pytest.mark.parametrize("run", range(3))
@pytest.mark.parametrize("group", ["ordinary", "large"])
@pytest.mark.parametrize("traced", [True, False])
def test_accounting_value_lifetime(
    report: Report, run: int, group: str, traced: bool
) -> None:
    names: list[str | None] = (
        [case.name for case in cost_cases() if case.group == group]
        if traced and group == "large"
        else [None]
    )
    mode = "traced" if traced else "native"
    before = (open_descriptors(), threading.active_count())

    for name in names:
        identity = f"cost_{mode}_{group}_{name or 'all'}_{run}"
        argv = [
            sys.executable,
            "-m",
            "tests.measurements.cost_workload",
            mode,
            group,
            report.revision,
            identity,
        ]
        if name is not None:
            argv.append(name)
        started = time.perf_counter()
        result = run_process(argv)
        elapsed = time.perf_counter() - started
        report.environment[identity] = result.stdout.decode()
        assert not result.timed_out
        assert not result.overflow
        assert result.code == 0, result.stderr.decode()
        assert result.stderr == b""
        payload = append_cost_report(report, result.stdout)
        environment = cast("dict[str, object]", payload["environment"])
        points = cast(
            "list[dict[str, int]]", json.loads(cast("str", environment["checkpoints"]))
        )
        assert len(points) == 4
        assert points[0]["cycle"] == 0
        assert points[-1]["cycle"] == (1000 if group == "ordinary" else 3)
        for resource in ("descriptors", "threads"):
            assert all(point[resource] == points[0][resource] for point in points)
        for point in points:
            assert (point["heap"] >= 0) is traced
            assert (point["rss"] >= 0) is not traced
            resource = "heap" if traced else "rss"
            limit = COST_RETAINED_LIMIT_BYTES if traced else COST_RSS_LIMIT_BYTES
            passed = point[resource] - points[0][resource] <= limit
            report.gates[f"{identity}_{point['cycle']}_{resource}_retained"] = passed
            assert passed, (identity, point, limit)
        report.samples.append(
            Sample(f"cost_{mode}_{group}_lifetime_process", run, 1, elapsed)
        )
        report.gates[f"{identity}_owned_resources_released"] = True

    assert (open_descriptors(), threading.active_count()) == before


@pytest.fixture(scope="module")
def report() -> Iterator[Report]:
    yield from report_lifecycle()


def leaves(total: int) -> list[JsonValue]:
    leaf = "a" * (LEAF_BYTES - 3)
    return [leaf for _ in range(total // LEAF_BYTES)]


def deep_extension(total: int) -> JsonObject:
    document: JsonObject = {"leaves": leaves(total)}
    for _ in range(DEFAULT_MAX_DEPTH - 3):
        document = {"n": document}
    return document


def too_deep() -> JsonObject:
    document: JsonObject = {}
    for _ in range(DEFAULT_MAX_DEPTH):
        document = {"n": document}
    return document


def accepted(action: Callable[[], object]) -> bool:
    try:
        action()
    except (ValidationError, JsonShapeError):
        return False
    return True


def parse(document: JsonObject) -> Callable[[], object]:
    return lambda: parse_json_object(document)


def dump(document: dict[str, object]) -> Callable[[], object]:
    policy = ToolPolicy.model_validate(document)
    return lambda: policy.model_dump(mode="json", exclude_unset=True)


def repeated(item: JsonValue, count: int) -> JsonObject:
    items: list[JsonValue] = [item] * count
    return {"k": items}


def many_keys(count: int) -> JsonObject:
    return {f"k{index}": 0 for index in range(count)}


DENSE: dict[str, JsonObject] = {
    "dense_zeros_1mib": repeated(0, 520_000),
    "dense_ascii_strings_1mib": repeated("a", 260_000),
    "dense_non_ascii_strings_1mib": repeated("é", 200_000),
    "dense_escaped_strings_1mib": repeated("a\n", 140_000),
    "dense_empty_objects_1mib": repeated({}, 340_000),
    "dense_keys_1mib": many_keys(90_000),
    "dense_floats_1mib": repeated(0.5, 250_000),
}


def validate(document: dict[str, object]) -> Callable[[], object]:
    return lambda: ToolPolicy.model_validate(document)


def protocol_error(details: JsonObject) -> Callable[[], object]:
    return lambda: KoraProtocolError("failed", "failed", False, details)


def config_schema(total: int) -> Callable[[], object]:
    document: JsonObject = {"type": "object", "x-leaves": leaves(total)}
    return lambda: parse_config_schema(document)


WORKLOADS: dict[str, tuple[Callable[[], object], int, bool, int]] = {
    **{
        name: (parse(document), 1, True, DENSE_MEASURED)
        for name, document in DENSE.items()
    },
    "dense_zeros_over_1mib": (parse(repeated(0, 600_000)), 1, False, DENSE_MEASURED),
    "dense_extension_1mib": (
        validate({"x-a": DENSE["dense_ascii_strings_1mib"]}),
        1,
        True,
        DENSE_MEASURED,
    ),
    "dump_patterns_250k": (dump({"allow": ["a"] * PATTERNS}), PATTERNS, True, MEASURED),
    "patterns_250k": (validate({"allow": ["a"] * PATTERNS}), PATTERNS, True, MEASURED),
    "extension_1mib_depth16": (
        validate({"x-a": deep_extension(MAX_CANONICAL_BYTES - 4096)}),
        1,
        True,
        MEASURED,
    ),
    "extension_over_1mib": (
        validate({"x-a": deep_extension(MAX_CANONICAL_BYTES + 4096)}),
        1,
        False,
        MEASURED,
    ),
    "refused_last_pattern": (
        validate({"allow": ["a"] * (PATTERNS - 1) + [1]}),
        PATTERNS,
        False,
        MEASURED,
    ),
    "protocol_error_details_1mib": (
        protocol_error({"leaves": leaves(MAX_CANONICAL_BYTES - 4096)}),
        1,
        True,
        MEASURED,
    ),
    "config_schema_64kib": (config_schema(CONFIG_SCHEMA_BYTES), 1, True, MEASURED),
}


def configuration_workloads() -> dict[str, tuple[Callable[[], object], int, bool, int]]:
    workloads: dict[str, tuple[Callable[[], object], int, bool, int]] = {}
    for shape, configurations in (("minimal", MINIMAL), ("populated", POPULATED)):
        for model, document in configurations.items():
            workloads[f"config_{model.__name__}_{shape}"] = (
                round_trip_action(model, document),
                1,
                True,
                MEASURED,
            )
    for size in (1, 1000, 10000):
        environment = {f"ENV_{index}": "value" for index in range(size)}
        aliases = {f"ENV_{index}": "ALIAS" for index in range(size)}
        egress = {"egress": ["host.example"] * size}
        command: dict[str, object] = {
            **MINIMAL[CommandConfig],
            "cmd": ["value"] * size,
            "env": environment,
            "secret_env": aliases,
            "success_exit_codes": [0] * size,
            "network_policy": egress,
        }
        server: dict[str, object] = {
            "type": "stdio",
            "args": ["value"] * size,
            "env": environment,
            "secret_env": aliases,
            "headers": {f"X-Header-{index}": "value" for index in range(size)},
            "network_policy": egress,
        }
        for model, document in ((CommandConfig, command), (McpServer, server)):
            for action_name, factory in (
                ("validate", validation_action),
                ("export", export_action),
            ):
                workloads[f"config_{model.__name__}_{size}_{action_name}"] = (
                    factory(model, document),
                    size,
                    True,
                    DENSE_MEASURED,
                )
        for valid in (True, False):
            duration = "1" * size + ("s" if valid else "!")
            resource = {
                **RESOURCE,
                "name": "/".join(["a"] * size) + ("" if valid else "/.."),
            }
            workloads[f"config_duration_{size}_{valid}"] = (
                validation_action(NodeLimits, {"timeout": duration}, valid),
                size,
                True,
                DENSE_MEASURED,
            )
            workloads[f"config_resource_path_{size}_{valid}"] = (
                validation_action(CommandResource, resource, valid),
                size,
                True,
                DENSE_MEASURED,
            )
        if size == 10000:
            refusals: dict[str, tuple[type[BaseModel], dict[str, object]]] = {
                "command_last_env": (
                    CommandConfig,
                    {**command, "env": {**environment, "LAST": False}},
                ),
                "server_last_alias": (
                    McpServer,
                    {**server, "secret_env": {**aliases, "LAST": "invalid"}},
                ),
                "command_many_errors": (
                    CommandConfig,
                    {**command, "cmd": [False] * size},
                ),
                "server_many_errors": (McpServer, {**server, "args": [False] * size}),
            }
            for name, (model, document) in refusals.items():
                workloads[f"config_refused_{name}"] = (
                    validation_action(model, document, False),
                    size,
                    True,
                    DENSE_MEASURED,
                )
    long_path = "/".join(["a\u2028\u2029"] * 10000)
    lexical_cases: dict[str, tuple[type[BaseModel], dict[str, object], bool]] = {
        "duration_final_lf": (NodeLimits, {"timeout": "1" * 10000 + "s\n"}, False),
        "resource_final_lf": (
            CommandResource,
            {**RESOURCE, "name": "/".join(["a"] * 10000) + "\n"},
            False,
        ),
        "resource_unicode_segments": (
            CommandResource,
            {**RESOURCE, "name": long_path},
            True,
        ),
        "resource_unicode_traversal": (
            CommandResource,
            {**RESOURCE, "name": long_path + "/../b"},
            False,
        ),
    }
    for name, (model, document, valid) in lexical_cases.items():
        workloads[f"config_lexical_{name}"] = (
            validation_action(model, document, valid),
            10000,
            True,
            DENSE_MEASURED,
        )
    extended = {**RESOURCE, "x-meta": deep_extension(MAX_CANONICAL_BYTES - 4096)}
    resources = [extended] * 64
    nested_command: dict[str, object] = {
        **MINIMAL[CommandConfig],
        "resources": resources,
    }
    for action_name, factory in (
        ("validate", validation_action),
        ("export", export_action),
    ):
        workloads[f"config_nested_extensions_64_{action_name}"] = (
            factory(CommandConfig, nested_command),
            64,
            True,
            DENSE_MEASURED,
        )
    for name, last in (
        ("resource", {**extended, "size": 262145}),
        ("extension", {**RESOURCE, "x-meta": too_deep()}),
    ):
        document = {**nested_command, "resources": [*resources[:-1], last]}
        workloads[f"config_refused_last_{name}"] = (
            validation_action(CommandConfig, document, False),
            64,
            True,
            DENSE_MEASURED,
        )
    return workloads


WORKLOADS.update(configuration_workloads())


@pytest.mark.parametrize("workload", sorted(WORKLOADS))
def test_ir_model_validation_cost(report: Report, workload: str) -> None:
    action, count, expected, measured = WORKLOADS[workload]
    for _ in range(WARM_UP):
        accepted(action)

    for repeat in range(measured):
        start = time.perf_counter()
        outcome = accepted(action)
        report.samples.append(
            Sample(workload, repeat, count, time.perf_counter() - start)
        )
        assert outcome is expected
    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    accepted(action)
    gc.collect()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    report.environment[f"{workload}_peak_bytes"] = peak
    retained = current - baseline
    report.environment[f"{workload}_retained_bytes"] = retained
    report.gates[f"{workload}_retained_at_baseline"] = retained < RETAINED_LIMIT_BYTES
    assert retained < RETAINED_LIMIT_BYTES


@pytest.mark.parametrize("run", range(3))
def test_ir_configuration_repeated_lifetime(report: Report, run: int) -> None:
    root = Path(__file__).parents[2]

    completed = subprocess.run(
        [sys.executable, "-m", "tests.measurements.ir_config_lifetime"],
        cwd=root,
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stderr == b""
    result = cast("dict[str, object]", json.loads(completed.stdout))
    report.environment[f"config_lifetime_{run}"] = completed.stdout.decode()
    checkpoints = cast("list[dict[str, int | float | None]]", result["checkpoints"])
    assert len(checkpoints) == 4
    for checkpoint in checkpoints:
        cycle = cast("int", checkpoint["cycles"])
        report.samples.append(
            Sample("config_lifetime", run, cycle, cast("float", checkpoint["seconds"]))
        )
        passed = cast("int", checkpoint["retained_bytes"]) < RETAINED_LIMIT_BYTES
        report.gates[f"config_lifetime_{run}_{cycle}_retained"] = passed
        assert passed
        assert checkpoint["live_models"] == 0
        assert checkpoint["threads"] == result["baseline_threads"]
        assert checkpoint["descriptors"] == result["baseline_descriptors"]
    extension = {"x-meta": deep_extension(MAX_CANONICAL_BYTES - 4096)}
    report.environment["config_nested_extensions_records"] = 64
    report.environment["config_nested_extensions_aggregate_bytes"] = 64 * len(
        json.dumps(extension, separators=(",", ":")).encode()
    )


FAILURES: tuple[dict[str, object], ...] = (
    {"allow": ["Read"], "unknown": "value"},
    {"deny": [7]},
    {"x-a": float("nan")},
    {"x-a": "a" * MAX_CANONICAL_BYTES},
    {"x-a": too_deep()},
)


def refuse_every_failure() -> None:
    for document in FAILURES:
        assert not accepted(validate(document))


def test_repeated_failures_return_to_baseline(report: Report) -> None:
    for _ in range(WARM_UP):
        refuse_every_failure()
    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]

    start = time.perf_counter()
    for _ in range(FAILURE_CYCLES):
        refuse_every_failure()
    elapsed = time.perf_counter() - start
    gc.collect()
    retained = tracemalloc.get_traced_memory()[0] - baseline
    tracemalloc.stop()

    report.samples.append(
        Sample("failure_cycles", 0, FAILURE_CYCLES * len(FAILURES), elapsed)
    )
    report.environment["failure_cycles_retained_bytes"] = retained
    report.gates["failure_cycles_return_to_baseline"] = retained < RETAINED_LIMIT_BYTES
    assert retained < RETAINED_LIMIT_BYTES


def test_shared_diagnostic_action_cost(report: Report) -> None:
    actions, lengths = cases()
    for fixture, fields in lengths.items():
        for field, value in fields.items():
            report.environment[f"diagnostic_{fixture}_{field}"] = value
    for action in actions:
        for _ in range(WARM_UP):
            exercise(action)

    for action in actions:
        for repeat in range(MEASURED):
            started = time.perf_counter()
            exercise(action)
            elapsed = time.perf_counter() - started
            report.samples.append(
                Sample(f"diagnostic_{action.name}", repeat, 1, elapsed)
            )

    assert len(actions) == 44


@pytest.mark.parametrize(
    ("repeat", "traced"),
    [(0, True), (0, False), (1, True), (1, False), (2, True), (2, False)],
)
def test_shared_diagnostic_resource_lifetime(
    report: Report,
    repeat: int,
    traced: bool,
) -> None:
    mode = "traced" if traced else "untraced"
    name = f"diagnostic_{mode}_{repeat}"
    before = (open_descriptors(), threading.active_count())
    argv = [
        sys.executable,
        "-m",
        "tests.measurements.diagnostic_workload",
        mode,
        report.revision,
        name,
    ]

    started = time.perf_counter()
    result = run_process(argv)
    elapsed = time.perf_counter() - started
    report.environment[f"{name}_stdout"] = result.stdout.decode()
    report.environment[f"{name}_stderr"] = result.stderr.decode()
    report.environment[f"{name}_exit"] = result.code
    report.samples.append(Sample(f"{name}_process", repeat, 1, elapsed))

    assert not result.timed_out
    assert not result.overflow
    assert result.code == 0
    assert result.stderr == b""
    assert (open_descriptors(), threading.active_count()) == before
    payload = Lifecycle.model_validate_json(result.stdout)
    assert payload.traced is traced
    assert payload.pid == result.pid
    assert payload.revision == report.revision
    assert payload.run == name
    assert payload.successes == 26_000
    assert payload.refusals == 18_000
    assert [p.cycle for p in payload.points] == list(range(0, 1001, 100))
    for resource in ("descriptors", "threads", "os_threads"):
        values = [getattr(p, resource) for p in payload.points]
        assert all(value == values[0] for value in values)
    if payload.platform == "linux":
        assert all(
            p.rss is not None and p.high_water is not None and p.os_threads is not None
            for p in payload.points
        )
    if traced:
        baseline = payload.points[0].heap
        assert baseline is not None
        assert all(
            p.heap is not None and p.heap - baseline < RETAINED_LIMIT_BYTES
            for p in payload.points
        )
    else:
        assert all(
            p.heap is None and p.peak is None and p.tracer is None
            for p in payload.points
        )
    report.gates[f"{name}_resources_released"] = True
