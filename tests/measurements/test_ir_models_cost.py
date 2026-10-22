import gc
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
from tests.measurements.harness import Report, Sample, report_lifecycle

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
