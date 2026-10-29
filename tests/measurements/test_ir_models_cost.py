import base64
import gc
import hashlib
import inspect
import json
import math
import os
import platform
import shutil
import sys
import tarfile
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

from kora.ir import (
    BranchConfig,
    CommandConfig,
    CommandResource,
    McpServer,
    NodeLimits,
    Retry,
    ToolPolicy,
)
from kora.ir.cost import PriceRevision, parse_money
from kora.ir.json import (
    DEFAULT_MAX_DEPTH,
    MAX_CANONICAL_BYTES,
    JsonObject,
    JsonShapeError,
    JsonValue,
    parse_json_object,
)
from kora.ir.pricing import estimate_usage_usd
from kora.protocols.errors import KoraProtocolError
from kora.protocols.metadata import parse_config_schema
from tests.ir.config_cases import MINIMAL, POPULATED, RESOURCE
from tests.ir.control_cases import CASE
from tests.ir.control_cases import MINIMAL as CONTROL_MINIMAL
from tests.ir.control_cases import POPULATED as CONTROL_POPULATED
from tests.ir.test_cost_models import rates_payload, revision_payload
from tests.measurements.cel_measurement_process import ProcessResult, run_process
from tests.measurements.cost_workload import CostCase, cost_cases, exercise_cost
from tests.measurements.diagnostic_workload import Lifecycle, cases, exercise
from tests.measurements.harness import (
    Report,
    Sample,
    new_report,
    open_descriptors,
    report_lifecycle,
)
from tests.measurements.ir_config_workload import (
    export_action,
    round_trip_action,
    validation_action,
)
from tests.measurements.ir_declaration_workload import (
    declaration_fixture_manifest,
    declaration_workloads,
)
from tests.measurements.ir_document_workload import (
    document_fixture_manifest,
    document_workloads,
)
from tests.measurements.ir_remaining_workload import (
    Workload,
    fixture_manifest,
    remaining_workloads,
)
from tests.measurements.ir_schema_workload import (
    schema_fixture_manifest,
    schema_workloads,
)
from tests.measurements.structured_output_workload import cases as structured_cases
from tests.measurements.structured_output_workload import (
    exercise as exercise_structured,
)
from tests.measurements.vcs_ref_workload import cases as vcs_ref_cases
from tests.measurements.vcs_ref_workload import exercise as exercise_vcs_ref

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


def require_cost_process(
    report: Report,
    identity: str,
    result: ProcessResult,
    elapsed: float,
    run: int,
    mode: str,
    group: str,
    batch: int,
    names: list[str],
) -> None:
    report.gates[f"{identity}_completed"] = False
    report.environment[f"{identity}_process"] = json.dumps(
        {
            "revision": report.revision,
            "run": run,
            "mode": mode,
            "group": group,
            "batch": batch,
            "cases": names,
            "pid": result.pid,
            "code": result.code,
            "timed_out": result.timed_out,
            "overflow": result.overflow,
            "elapsed_seconds": elapsed,
            "stdout_base64": base64.b64encode(result.stdout).decode("ascii"),
            "stderr_base64": base64.b64encode(result.stderr).decode("ascii"),
            "output_scope": "all_runner_retained_bytes_capped_per_stream",
        }
    )
    assert not result.timed_out
    assert not result.overflow
    assert result.code == 0, (identity, result.code)
    assert result.stderr == b""


def test_cost_process_failure_preserves_lossless_evidence() -> None:
    created = new_report()
    identity = "cost_failure_evidence"
    source = (
        "import os,sys;os.write(1,b'partial\\xff');"
        "os.write(2,b'failure\\x00\\xfe');sys.exit(7)"
    )
    started = time.perf_counter()
    result = run_process([sys.executable, "-I", "-c", source])

    with pytest.raises(AssertionError):
        require_cost_process(
            created,
            identity,
            result,
            time.perf_counter() - started,
            0,
            "timing",
            "large",
            1,
            ["snapshot_bytes_1048576_validate"],
        )

    envelope = cast(
        "dict[str, object]",
        json.loads(cast("str", created.environment[f"{identity}_process"])),
    )
    assert base64.b64decode(cast("str", envelope["stdout_base64"])) == b"partial\xff"
    assert (
        base64.b64decode(cast("str", envelope["stderr_base64"])) == b"failure\x00\xfe"
    )
    assert envelope["pid"] == result.pid
    assert envelope["code"] == 7
    assert envelope["cases"] == ["snapshot_bytes_1048576_validate"]
    assert not created.gates[f"{identity}_completed"]
    assert created.samples == []


def accounting_file(path: Path) -> tuple[bytes, bool] | None:
    try:
        with path.open("rb") as source:
            data = source.read(262_145)
    except FileNotFoundError:
        return None
    return data[:262_144], len(data) > 262_144


def accounting_cpu_quota() -> dict[str, object]:
    membership = accounting_file(Path("/proc/self/cgroup"))
    if membership is None or membership[1]:
        return {"status": "unavailable", "reason": "no_bounded_cgroup_membership"}
    unified = [
        line[3:]
        for line in membership[0].decode().splitlines()
        if line.startswith("0::")
    ]
    if len(unified) != 1 or ".." in Path(unified[0]).parts:
        return {"status": "unavailable", "reason": "unresolved_cgroup_v2_membership"}
    root = Path("/sys/fs/cgroup")
    current = root / unified[0].lstrip("/")
    limits: list[float] = []
    for _ in range(32):
        value = accounting_file(current / "cpu.max")
        if value is None or value[1]:
            return {"status": "unavailable", "reason": "unresolved_visible_cpu_max"}
        quota, period = value[0].decode().split()
        if quota != "max":
            limits.append(int(quota) / int(period))
        if current == root:
            return {
                "status": "available",
                "scope": "minimum_visible_cgroup_v2_ancestor_quota",
                "cpu_quota": min(limits) if limits else None,
                "unlimited": not limits,
            }
        current = current.parent
    return {"status": "unavailable", "reason": "visible_hierarchy_exceeds_32_levels"}


def accounting_environment(report: Report) -> None:
    evidence: dict[str, object] = {
        "scope": "parent_host_once_before_child_action_timing",
        "pid": os.getpid(),
        "platform": sys.platform,
        "release": platform.release(),
        "machine": platform.machine(),
        "python": sys.version,
        "executable": sys.executable,
        "revision": report.revision,
        "cpu_count": os.cpu_count(),
    }
    if sys.platform == "darwin":
        model = run_process(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"])
        evidence["cpu_model"] = {
            "source": "sysctl_machdep.cpu.brand_string",
            "code": model.code,
            "timed_out": model.timed_out,
            "overflow": model.overflow,
            "value": model.stdout.decode(errors="replace").strip()[:256]
            if model.code == 0 and not model.timed_out and not model.overflow
            else None,
        }
        evidence["effective_cpu_quota"] = {"status": "not_applicable_darwin"}
    else:
        cpuinfo = accounting_file(Path("/proc/cpuinfo"))
        models = (
            []
            if cpuinfo is None
            else [
                line.split(":", 1)[1].strip()[:256]
                for line in cpuinfo[0].decode(errors="replace").splitlines()
                if line.startswith("model name") and ":" in line
            ]
        )
        evidence["cpu_model"] = {
            "source": "/proc/cpuinfo_first_262144_bytes",
            "value": models[0] if models else None,
            "truncated": cpuinfo is not None and cpuinfo[1],
        }
        evidence["cpu_affinity_count"] = len(os.sched_getaffinity(0))
        evidence["effective_cpu_quota"] = accounting_cpu_quota()
    identities: list[dict[str, object]] = []
    for path in (
        Path(__file__),
        Path(inspect.getfile(cost_cases)),
        Path(inspect.getfile(PriceRevision)),
        Path(inspect.getfile(estimate_usage_usd)),
    ):
        source = accounting_file(path)
        identities.append(
            {
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(source[0]).hexdigest()
                if source is not None and not source[1]
                else None,
                "complete": source is not None and not source[1],
            }
        )
    evidence["sources"] = identities
    report.environment["accounting_host"] = json.dumps(evidence)
    inventory: dict[str, list[str]] = {"ordinary": [], "large": []}
    for case in cost_cases():
        inventory[case.group].append(case.name)
    report.environment["accounting_case_inventory"] = json.dumps(inventory)


@pytest.mark.parametrize("run", range(3))
@pytest.mark.parametrize("group", ["ordinary", "large"])
def test_accounting_value_cost(report: Report, run: int, group: str) -> None:
    batches = [[case.name for case in cost_cases() if case.group == group]]
    if group == "large":
        names = batches[0]
        batches = [[name for name in names if not name.startswith("snapshot_bytes")]]
        batches.extend([name] for name in names if name.startswith("snapshot_bytes"))
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
        require_cost_process(
            report, identity, result, elapsed, run, "timing", group, batch_index, names
        )
        report.environment[identity] = result.stdout.decode()
        payload = append_cost_report(report, result.stdout)
        environment = cast("dict[str, object]", payload["environment"])
        assert environment["operations_per_batch"] == 1
        summaries = cast("dict[str, dict[str, float]]", payload["summary"])
        counts = {sample.workload: sample.count for sample in report.samples}
        for name, summary in summaries.items():
            seconds = summary["p95_us_per_record"] * counts[name] / 1_000_000
            captured = math.isfinite(seconds) and seconds >= 0
            report.gates[f"{identity}_{name}_p95_captured"] = captured
            assert captured, (name, seconds)
        report.samples.append(Sample(f"cost_{group}_timing_process", run, 1, elapsed))
        report.gates[f"{identity}_completed"] = True

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
    inventory = cast(
        "dict[str, list[str]]",
        json.loads(cast("str", report.environment["accounting_case_inventory"])),
    )

    for batch, name in enumerate(names):
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
        require_cost_process(
            report,
            identity,
            result,
            elapsed,
            run,
            mode,
            group,
            batch,
            inventory[group] if name is None else [name],
        )
        report.environment[identity] = result.stdout.decode()
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
            report.diagnostic(
                f"{identity}_{point['cycle']}_{resource}_retained_bytes",
                point[resource] - points[0][resource],
                limit,
            )
            report.gates[f"{identity}_{point['cycle']}_{resource}_observed"] = True
        report.samples.append(
            Sample(f"cost_{mode}_{group}_lifetime_process", run, 1, elapsed)
        )
        report.gates[f"{identity}_owned_resources_released"] = True
        report.gates[f"{identity}_completed"] = True

    assert (open_descriptors(), threading.active_count()) == before


@pytest.fixture(scope="module")
def report(request: pytest.FixtureRequest) -> Iterator[Report]:
    for report in report_lifecycle():
        accounting_environment(report)
        report.environment["selected_test_nodes"] = json.dumps(
            [item.nodeid for item in request.session.items]
        )
        report.environment["uv_lock_sha256"] = hashlib.sha256(
            Path("uv.lock").read_bytes()
        ).hexdigest()
        report.environment["remaining_fixture_manifest"] = fixture_manifest()
        report.environment["declaration_fixture_manifest"] = (
            declaration_fixture_manifest()
        )
        report.environment["document_fixture_manifest"] = document_fixture_manifest()
        report.environment["schema_fixture_manifest"] = schema_fixture_manifest()
        yield report


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


WORKLOADS: dict[str, Workload] = {
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


def validate_and_export(
    prefix: str, model: type[BaseModel], document: dict[str, object], count: int
) -> dict[str, Workload]:
    return {
        f"{prefix}_{action_name}": (
            factory(model, document),
            count,
            True,
            DENSE_MEASURED,
        )
        for action_name, factory in (
            ("validate", validation_action),
            ("export", export_action),
        )
    }


def configuration_workloads() -> dict[str, Workload]:
    workloads: dict[str, Workload] = {}
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
            workloads.update(
                validate_and_export(
                    f"config_{model.__name__}_{size}", model, document, size
                )
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
    workloads.update(
        validate_and_export(
            "config_nested_extensions_64", CommandConfig, nested_command, 64
        )
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


def control_workloads() -> dict[str, Workload]:
    workloads: dict[str, Workload] = {}
    for shape, configurations in (
        ("minimal", CONTROL_MINIMAL),
        ("populated", CONTROL_POPULATED),
    ):
        for model, document in configurations.items():
            workloads[f"control_{model.__name__}_{shape}"] = (
                round_trip_action(model, document),
                1,
                True,
                MEASURED,
            )
    for size in (1, 1000, 10000):
        branch_cases: dict[str, object] = {"cases": [CASE] * size}
        targets = ["target"] * size
        branch_targets: dict[str, object] = {"cases": [{"when": "true", "to": targets}]}
        for name, document in (("cases", branch_cases), ("targets", branch_targets)):
            workloads.update(
                validate_and_export(
                    f"control_branch_{name}_{size}", BranchConfig, document, size
                )
            )
        invalid_case: dict[str, object] = {"when": "true", "to": ["a" * 64 + "\n"]}
        refusals: dict[str, dict[str, object]] = {
            "last_case": {"cases": [CASE] * (size - 1) + [invalid_case]},
            "many_cases": {"cases": [invalid_case] * size},
            "last_target": {
                "cases": [{"when": "true", "to": [*targets[:-1], "$fail_attempt\n"]}]
            },
            "many_targets": {
                "cases": [
                    {
                        "when": "true",
                        "to": ["a" * 64 + "\n", "$fail_attempt\n"] * (size // 2)
                        + ["a\n"] * (size % 2),
                    }
                ]
            },
        }
        for name, document in refusals.items():
            workloads[f"control_branch_refused_{name}_{size}"] = (
                validation_action(BranchConfig, document, False),
                size,
                True,
                DENSE_MEASURED,
            )
    branch_else: dict[str, object] = {"cases": [CASE], "else": ["target"] * 10000}
    workloads.update(
        validate_and_export("control_else_10000", BranchConfig, branch_else, 10000)
    )
    workloads["control_else_refused_last_10000"] = (
        validation_action(
            BranchConfig,
            {"cases": [CASE], "else": ["target"] * 9999 + ["$fail_attempt\n"]},
            False,
        ),
        10000,
        True,
        DENSE_MEASURED,
    )
    for field in ("retry_on", "non_retryable"):
        for name, entries in (
            ("invalid", ["unknown"] * 10000),
            ("duplicate", ["timeout"] * 10000),
        ):
            workloads[f"control_retry_{field}_{name}_10000"] = (
                validation_action(Retry, {field: entries}, False),
                10000,
                True,
                DENSE_MEASURED,
            )
    extended = {**CASE, "x-meta": deep_extension(MAX_CANONICAL_BYTES - 4096)}
    nested_branch: dict[str, object] = {
        "cases": [extended, extended],
        "else": ["target"],
    }
    workloads.update(
        validate_and_export("control_case_extensions_2", BranchConfig, nested_branch, 2)
    )
    for name, extension in (
        ("oversize", deep_extension(MAX_CANONICAL_BYTES + 4096)),
        ("depth", too_deep()),
    ):
        workloads[f"control_case_extension_last_{name}"] = (
            validation_action(
                BranchConfig,
                {"cases": [extended, {**CASE, "x-meta": extension}]},
                False,
            ),
            2,
            True,
            DENSE_MEASURED,
        )
    return workloads


WORKLOADS.update(configuration_workloads())
WORKLOADS.update(control_workloads())
WORKLOADS.update(remaining_workloads())
WORKLOADS.update(declaration_workloads())
WORKLOADS.update(document_workloads())
WORKLOADS.update(schema_workloads())


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
    report.diagnostic(
        f"{workload}_retained_bytes", retained, RETAINED_LIMIT_BYTES, allow_equal=False
    )
    report.gates[f"{workload}_memory_observed"] = True


def assert_clean_exit(result: ProcessResult) -> None:
    assert not result.timed_out
    assert not result.overflow
    assert result.code == 0, result.stderr.decode()
    assert result.stderr == b""


@pytest.mark.parametrize("run", range(3))
def test_ir_configuration_repeated_lifetime(report: Report, run: int) -> None:
    before = (open_descriptors(), threading.active_count())
    completed = run_process(
        [sys.executable, "-m", "tests.measurements.ir_config_lifetime"],
        deadline_seconds=180,
    )

    assert_clean_exit(completed)
    assert (open_descriptors(), threading.active_count()) == before
    result = cast("dict[str, object]", json.loads(completed.stdout))
    report.environment[f"config_lifetime_{run}"] = completed.stdout.decode()
    checkpoints = cast("list[dict[str, int | float | None]]", result["checkpoints"])
    assert len(checkpoints) == 4
    for checkpoint in checkpoints:
        cycle = cast("int", checkpoint["cycles"])
        report.samples.append(
            Sample("config_lifetime", run, cycle, cast("float", checkpoint["seconds"]))
        )
        report.diagnostic(
            f"config_lifetime_{run}_{cycle}_retained_bytes",
            cast("int", checkpoint["retained_bytes"]),
            RETAINED_LIMIT_BYTES,
            allow_equal=False,
        )
        report.gates[f"config_lifetime_{run}_{cycle}_resources_released"] = (
            checkpoint["live_models"] == 0
        )
        assert checkpoint["live_models"] == 0
        assert checkpoint["threads"] == result["baseline_threads"]
        assert checkpoint["descriptors"] == result["baseline_descriptors"]
        assert checkpoint["os_threads"] == result["baseline_os_threads"]
        if sys.platform == "linux":
            assert checkpoint["current_rss_bytes"] is not None
            assert checkpoint["peak_rss_bytes"] is not None
            assert checkpoint["os_threads"] is not None
    extension = {"x-meta": deep_extension(MAX_CANONICAL_BYTES - 4096)}
    report.environment["config_nested_extensions_records"] = 64
    report.environment["config_nested_extensions_aggregate_bytes"] = 64 * len(
        json.dumps(extension, separators=(",", ":")).encode()
    )


@pytest.mark.parametrize("run", range(3))
def test_ir_schema_repeated_lifetime(report: Report, run: int) -> None:
    before = (open_descriptors(), threading.active_count())

    completed = run_process(
        [
            sys.executable,
            "-m",
            "tests.measurements.ir_schema_lifetime",
            report.revision,
        ],
        deadline_seconds=300,
    )
    report.environment[f"schema_lifetime_{run}"] = completed.stdout.decode()
    report.environment[f"schema_lifetime_{run}_stderr"] = completed.stderr.decode()
    report.environment[f"schema_lifetime_{run}_exit"] = completed.code
    report.environment[f"schema_lifetime_{run}_timeout"] = completed.timed_out
    report.environment[f"schema_lifetime_{run}_overflow"] = completed.overflow
    after = (open_descriptors(), threading.active_count())
    report.environment[f"schema_lifetime_{run}_parent_descriptors_before"] = before[0]
    report.environment[f"schema_lifetime_{run}_parent_descriptors_after"] = after[0]
    report.environment[f"schema_lifetime_{run}_parent_threads_before"] = before[1]
    report.environment[f"schema_lifetime_{run}_parent_threads_after"] = after[1]

    assert after == before
    assert_clean_exit(completed)
    result = cast("dict[str, object]", json.loads(completed.stdout))
    assert result["pid"] == completed.pid
    assert result["revision"] == report.revision
    assert result["platform"] == sys.platform
    assert result["python"] == sys.version.split()[0]
    assert result["cycles"] == 1000
    assert result["warm_up"] == 3
    assert result["action_counts"] == {
        "validation": 334,
        "serialization": 333,
        "render_utf8": 333,
    }
    assert result["class_schemas_unchanged"] is True
    manifest = cast("dict[str, object]", result["schema_manifest"])
    assert result["class_schema_inventory"] == manifest["class_schema_inventory"]
    assert manifest == json.loads(
        cast("str", report.environment["schema_fixture_manifest"])
    )
    checkpoints = cast("list[dict[str, int | float | None]]", result["checkpoints"])
    assert [checkpoint["cycles"] for checkpoint in checkpoints] == [250, 500, 750, 1000]
    for checkpoint in checkpoints:
        cycle = cast("int", checkpoint["cycles"])
        report.samples.append(
            Sample("schema_lifetime", run, cycle, cast("float", checkpoint["seconds"]))
        )
        passed = (
            checkpoint["live_generators"] == 0
            and checkpoint["threads"] == result["baseline_threads"]
            and checkpoint["descriptors"] == result["baseline_descriptors"]
            and checkpoint["os_threads"] == result["baseline_os_threads"]
        )
        report.gates[f"schema_lifetime_{run}_{cycle}_resources_released"] = passed
        assert passed
        assert checkpoint["threads"] == result["baseline_threads"]
        assert checkpoint["descriptors"] == result["baseline_descriptors"]
        assert checkpoint["os_threads"] == result["baseline_os_threads"]
        if sys.platform == "linux":
            assert checkpoint["current_rss_bytes"] is not None
            assert checkpoint["peak_rss_bytes"] is not None
            assert checkpoint["os_threads"] is not None


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
    report.diagnostic(
        "failure_cycles_retained_bytes",
        retained,
        RETAINED_LIMIT_BYTES,
        allow_equal=False,
    )
    report.gates["failure_cycles_memory_observed"] = True


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


def run_lifecycle_child(
    report: Report, module: str, mode: str, name: str, repeat: int, count: int
) -> ProcessResult:
    argv = [sys.executable, "-m", module, mode, report.revision, name]
    started = time.perf_counter()
    result = run_process(argv)
    elapsed = time.perf_counter() - started
    report.environment[f"{name}_stdout"] = result.stdout.decode()
    report.environment[f"{name}_stderr"] = result.stderr.decode()
    report.environment[f"{name}_exit"] = result.code
    report.samples.append(Sample(f"{name}_process", repeat, count, elapsed))
    return result


def released_lifecycle(
    report: Report,
    result: ProcessResult,
    before: tuple[int, int],
    traced: bool,
    name: str,
) -> Lifecycle:
    assert_clean_exit(result)
    assert (open_descriptors(), threading.active_count()) == before
    payload = Lifecycle.model_validate_json(result.stdout)
    assert payload.traced is traced
    assert payload.pid == result.pid
    assert payload.revision == report.revision
    assert payload.run == name
    return payload


def assert_lifecycle_resources(
    report: Report, name: str, payload: Lifecycle, traced: bool
) -> None:
    for counter in ("descriptors", "threads", "os_threads"):
        values = [getattr(point, counter) for point in payload.points]
        assert all(value == values[0] for value in values)
    if payload.platform == "linux":
        assert all(
            point.rss is not None
            and point.high_water is not None
            and point.os_threads is not None
            for point in payload.points
        )
    if traced:
        baseline = payload.points[0].heap
        assert baseline is not None
        for point in payload.points:
            assert point.heap is not None
            report.diagnostic(
                f"{name}_{point.cycle}_retained_bytes",
                point.heap - baseline,
                RETAINED_LIMIT_BYTES,
                allow_equal=False,
            )
    else:
        assert all(
            point.heap is None and point.peak is None and point.tracer is None
            for point in payload.points
        )


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

    result = run_lifecycle_child(
        report, "tests.measurements.diagnostic_workload", mode, name, repeat, 1
    )

    payload = released_lifecycle(report, result, before, traced, name)
    assert payload.successes == 26_000
    assert payload.refusals == 18_000
    assert [p.cycle for p in payload.points] == list(range(0, 1001, 100))
    assert_lifecycle_resources(report, name, payload, traced)
    report.gates[f"{name}_resources_released"] = True


def test_vcs_ref_action_cost(report: Report) -> None:
    actions, manifest = vcs_ref_cases()
    report.environment["vcs_ref_fixture_manifest"] = json.dumps(
        manifest, sort_keys=True
    )
    for case in actions:
        for _ in range(WARM_UP):
            exercise_vcs_ref(case)

    for case in actions:
        for repeat in range(DENSE_MEASURED):
            started = time.perf_counter()
            exercise_vcs_ref(case)
            elapsed = time.perf_counter() - started
            report.samples.append(Sample(case.name, repeat, 1, elapsed))
        gc.collect()
        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]
        exercise_vcs_ref(case)
        gc.collect()
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        retained = current - baseline
        report.environment[f"{case.name}_peak_bytes"] = peak
        report.environment[f"{case.name}_retained_bytes"] = retained
        report.diagnostic(
            f"{case.name}_retained_bytes",
            retained,
            RETAINED_LIMIT_BYTES,
            allow_equal=False,
        )
        report.gates[f"{case.name}_memory_observed"] = True


def test_structured_output_action_cost(report: Report) -> None:
    actions, manifest = structured_cases()
    report.environment["structured_output_fixture_manifest"] = json.dumps(manifest)
    for case in actions:
        for _ in range(WARM_UP):
            exercise_structured(case)

    for case in actions:
        for repeat in range(DENSE_MEASURED):
            started = time.perf_counter()
            exercise_structured(case)
            report.samples.append(
                Sample(case.name, repeat, 1, time.perf_counter() - started)
            )
        gc.collect()
        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]
        exercise_structured(case)
        gc.collect()
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        report.environment[f"{case.name}_peak_bytes"] = peak
        report.environment[f"{case.name}_retained_bytes"] = retained - baseline
        report.diagnostic(
            f"{case.name}_retained_bytes",
            retained - baseline,
            RETAINED_LIMIT_BYTES,
            allow_equal=False,
        )
        report.gates[f"{case.name}_memory_observed"] = True


@pytest.mark.parametrize(
    ("repeat", "traced"),
    [(0, True), (0, False), (1, True), (1, False), (2, True), (2, False)],
)
def test_structured_output_validation_lifetime(
    report: Report, repeat: int, traced: bool
) -> None:
    mode = "traced" if traced else "untraced"
    name = f"structured_output_{mode}_{repeat}"
    before = (open_descriptors(), threading.active_count())

    result = run_lifecycle_child(
        report, "tests.measurements.structured_output_workload", mode, name, repeat, 500
    )

    payload = released_lifecycle(report, result, before, traced, name)
    assert payload.successes == 248
    assert payload.refusals == 252
    assert [point.cycle for point in payload.points] == list(range(0, 501, 25))
    assert len(payload.retaining_paths) == 2
    assert_lifecycle_resources(report, name, payload, traced)
    report.gates[f"{name}_lifecycle_assertions_passed"] = True


@pytest.mark.parametrize(
    ("repeat", "traced"),
    [(0, True), (0, False), (1, True), (1, False), (2, True), (2, False)],
)
def test_vcs_ref_validation_lifetime(report: Report, repeat: int, traced: bool) -> None:
    mode = "traced" if traced else "untraced"
    name = f"vcs_ref_{mode}_{repeat}"
    before = (open_descriptors(), threading.active_count())

    result = run_lifecycle_child(
        report, "tests.measurements.vcs_ref_workload", mode, name, repeat, 500
    )

    payload = released_lifecycle(report, result, before, traced, name)
    assert payload.successes == 284
    assert payload.refusals == 216
    assert [point.cycle for point in payload.points] == list(range(0, 501, 25))
    assert len(payload.retaining_paths) == 2
    assert_lifecycle_resources(report, name, payload, traced)
    report.gates[f"{name}_lifecycle_assertions_passed"] = True
    report.environment[f"{name}_native_growth_review"] = "required"


@pytest.mark.parametrize("repeat", range(3))
def test_vcs_ref_cold_public_import(report: Report, repeat: int) -> None:
    base = "41ea853883201665adaeb7d730da9d15be148456"
    before = (open_descriptors(), threading.active_count())
    source = Path(__file__).parents[2] / "src"

    with TemporaryDirectory(prefix="kora-vcs-ref-import-") as directory:
        temporary = Path(directory)
        archive = temporary / "base.tar"
        archived = run_process(
            ["git", "archive", f"--output={archive}", base, "src/kora"]
        )
        assert archived.code == 0, archived.stderr.decode()
        assert not archived.timed_out
        assert not archived.overflow
        with tarfile.open(archive) as bundle:
            bundle.extractall(temporary, filter="data")
        copied_head = temporary / "head"
        shutil.copytree(
            source,
            copied_head,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        for name, selected in (("base", temporary / "src"), ("head", copied_head)):
            completed = run_process(
                [
                    sys.executable,
                    "-m",
                    "tests.measurements.vcs_ref_import",
                    str(selected),
                ]
            )
            report.environment[f"vcs_import_{name}_{repeat}"] = (
                completed.stdout.decode()
            )
            assert_clean_exit(completed)
            measured = cast("dict[str, object]", json.loads(completed.stdout))
            assert measured["descriptors_before"] == measured["descriptors_after"]
            assert measured["threads_before"] == measured["threads_after"]
            if measured["platform"] == "linux":
                assert measured["rss_before"] is not None
                assert measured["rss_after"] is not None
            seconds = measured["seconds"]
            assert isinstance(seconds, float)
            report.samples.append(Sample(f"vcs_import_{name}", repeat, 1, seconds))

    assert not temporary.exists()
    assert (open_descriptors(), threading.active_count()) == before
    report.gates[f"vcs_import_{repeat}_resources_released"] = True
