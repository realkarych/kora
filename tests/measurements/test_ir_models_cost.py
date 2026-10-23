import gc
import sys
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator

import pytest
from pydantic import ValidationError

from kora.ir import ToolPolicy
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
from tests.measurements.cel_measurement_process import run_process
from tests.measurements.diagnostic_workload import Lifecycle, cases, exercise
from tests.measurements.harness import (
    Report,
    Sample,
    open_descriptors,
    report_lifecycle,
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
