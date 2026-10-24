import gc
import json
import time
import tracemalloc
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from kora.conformance.capture import captured
from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.observability import LoggingSettings, ProcessDiagnostics
from kora.protocols.identity import GrantBinding
from kora.protocols.secret_value import SecretScope
from tests.measurements.harness import (
    Report,
    Sample,
    current_rss_bytes,
    peak_rss_bytes,
    report_lifecycle,
)
from tests.measurements.harness_workload import (
    WORKLOADS,
    build,
    encoded_bytes,
    maximal_result,
    traced_batch,
    traced_cycles,
)
from tests.support.heap import recurring_lines, snapshot, top_growth
from tests.support.observability import run_python

pytestmark = pytest.mark.measurement

INDEPENDENT_RUNS: int = 3
MEASURED: int = 30
WARM_UP: int = 5
GRANTS: int = 64
REGISTRATION_CYCLES: int = 3
HEAD_BYTES: int = 8
TRACED_CYCLES: int = 2
RETAINED_FLOOR_BYTES: int = 4096
HEAP_GROWTH_LIMIT_BYTES: int = 262_144
HARNESS_WARM_UP: int = 3
HARNESS_MEASURED: int = 30
HARNESS_DENSE_MEASURED: int = 10
HARNESS_RETAINED_LIMIT_BYTES: int = 65_536
IMPORT_PROBE: str = (
    "import json, sys, time\n"
    "start = time.perf_counter()\n"
    "import kora.protocols.common, kora.protocols.identity, kora.protocols.secrets\n"
    "print(json.dumps({'seconds': time.perf_counter() - start, "
    "'modules': len(sys.modules)}))\n"
)
REGISTRATION_WORKLOADS: dict[str, tuple[int, int, bool]] = {
    "plain_8b": (250, 8, False),
    "escaped_8b": (85, 8, True),
    "plain_1kib": (32, 1024, False),
    "escaped_1kib": (12, 1024, True),
    "plain_4kib": (15, 4096, False),
    "escaped_4kib": (8, 4000, True),
}


@pytest.fixture(scope="module")
def report() -> Iterator[Report]:
    yield from report_lifecycle()


def utc_now() -> datetime:
    return datetime.now(UTC)


def value_of(index: int, size: int, escaped: bool) -> str:
    head = ('"' if escaped else "v") + f"{index:0{HEAD_BYTES - 1}d}"
    filler = "".join(
        '"' if escaped and position % 64 == 0 else "x"
        for position in range(size - HEAD_BYTES)
    )
    return head + filler


def fresh_copies(values: list[str]) -> list[str]:
    return [value[:-1] + value[-1:] for value in values]


def retained_limit(count: int, size: int) -> int:
    return max(count * size // 4, RETAINED_FLOOR_BYTES)


def expected_forms(count: int, escaped: bool) -> int:
    return count * (2 if escaped else 1)


def binding_document() -> bytes:
    process = {
        "host_id": "host",
        "boot_id": "boot",
        "pid_namespace": "pidns",
        "pid": 1000,
        "start_time": 1,
        "leads_group": True,
    }
    grants = [
        {
            "dispatcher_id": f"dispatcher-{number}",
            "worker_id": "worker",
            "incarnation_id": "incarnation",
            "lease_epoch": number,
            "execution_domain_id": "domain",
            "process": process,
        }
        for number in range(GRANTS)
    ]
    return json.dumps(grants).encode("utf-8")


def test_protocol_import_cost(report: Report) -> None:
    for run in range(INDEPENDENT_RUNS):
        result = run_python(["-I", "-c", IMPORT_PROBE], {})

        assert result.code == 0, result.stderr
        measured = json.loads(result.stdout)
        report.samples.append(Sample("cold_import", run, 1, measured["seconds"]))
        report.environment[f"run_{run}_modules"] = measured["modules"]


def test_grant_binding_validation_cost(report: Report) -> None:
    document = binding_document()
    for _ in range(WARM_UP):
        [GrantBinding.model_validate(item) for item in json.loads(document)]
    tracemalloc.start()

    for repeat in range(MEASURED):
        start = time.perf_counter()
        [GrantBinding.model_validate(item) for item in json.loads(document)]
        report.samples.append(
            Sample("grant_bindings_64", repeat, GRANTS, time.perf_counter() - start)
        )
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    report.environment["grant_bindings_peak_bytes"] = peak


def admit_and_release(
    diagnostics: ProcessDiagnostics, values: list[str]
) -> tuple[int, int]:
    scope = SecretScope(diagnostics)
    for value in values:
        scope.admit(value)
    registry = diagnostics.registry
    state = registry.count(), registry.retained_bytes()
    scope.release()
    return state


@pytest.mark.parametrize("workload", sorted(REGISTRATION_WORKLOADS))
def test_secret_registration_cost(report: Report, workload: str) -> None:
    count, size, escaped = REGISTRATION_WORKLOADS[workload]
    values = [value_of(index, size, escaped) for index in range(count)]
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), "measurement", utc_now
    ) as capture:
        diagnostics = capture.diagnostics
        forms, retained = admit_and_release(diagnostics, values)

        for repeat in range(REGISTRATION_CYCLES):
            start = time.perf_counter()
            forms, retained = admit_and_release(diagnostics, values)
            report.samples.append(
                Sample(workload, repeat, count, time.perf_counter() - start)
            )
        gc.collect()
        tracemalloc.start()
        traced = [tracemalloc.get_traced_memory()[0]]
        for _ in range(TRACED_CYCLES):
            admit_and_release(diagnostics, fresh_copies(values))
            gc.collect()
            traced.append(tracemalloc.get_traced_memory()[0])
        tracemalloc.stop()
        growth = traced[-1] - traced[0]
        later_growth = traced[-1] - traced[1]

        report.environment[f"{workload}_forms"] = forms
        report.environment[f"{workload}_retained_bytes"] = retained
        report.environment[f"{workload}_heap_growth_bytes"] = growth
        report.environment[f"{workload}_later_heap_growth_bytes"] = later_growth
        assert len(set(values)) == count
        assert forms == expected_forms(count, escaped)
        assert diagnostics.registry.count() == 0
        assert growth < HEAP_GROWTH_LIMIT_BYTES
        assert later_growth < retained_limit(count, size)


def peak_scope(workload: str, count: int) -> str:
    kind = "result" if workload.startswith("session_result") else "event"
    return f"{count} {kind}{'' if count == 1 else 's'} validated per action"


def harness_repeats(workload: str) -> int:
    return HARNESS_MEASURED if "mixed_small" in workload else HARNESS_DENSE_MEASURED


@pytest.mark.parametrize("workload", WORKLOADS)
def test_harness_model_validation_cost(report: Report, workload: str) -> None:
    action, count = build(workload)
    report.environment[f"{workload}_rss_start_bytes"] = current_rss_bytes()
    process_peak_before = peak_rss_bytes()
    for _ in range(HARNESS_WARM_UP):
        action()
    report.environment[f"{workload}_rss_warm_bytes"] = current_rss_bytes()

    for repeat in range(harness_repeats(workload)):
        start = time.perf_counter()
        action()
        report.samples.append(
            Sample(workload, repeat, count, time.perf_counter() - start)
        )
    cycles = traced_cycles(workload)
    batch = traced_batch(workload)
    tracemalloc.start()
    before = snapshot()
    for _ in range((cycles // 2) * batch):
        action()
    middle = snapshot()
    for _ in range((cycles - cycles // 2) * batch):
        action()
    after = snapshot()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    report.environment[f"{workload}_rss_end_bytes"] = current_rss_bytes()
    report.environment[f"{workload}_process_peak_rss_bytes"] = peak_rss_bytes()
    report.environment[f"{workload}_process_peak_rss_growth_bytes"] = (
        peak_rss_bytes() - process_peak_before
    )

    retained = sum(stat.size_diff for stat in after.compare_to(before, "lineno"))
    lines = recurring_lines(before, middle, after, cycles * batch)
    recurring = sum(stat.size_diff for stat in lines)
    report.environment[f"{workload}_top_growth"] = "; ".join(top_growth(lines, 3))
    report.environment[f"{workload}_peak_bytes"] = peak
    report.environment[f"{workload}_peak_scope"] = peak_scope(workload, count)
    report.environment[f"{workload}_retained_bytes"] = retained
    report.environment[f"{workload}_recurring_growth_bytes"] = recurring
    report.gates[f"{workload}_retained_at_baseline"] = (
        retained < HARNESS_RETAINED_LIMIT_BYTES
        and recurring < HARNESS_RETAINED_LIMIT_BYTES
    )
    assert retained < HARNESS_RETAINED_LIMIT_BYTES
    assert recurring < HARNESS_RETAINED_LIMIT_BYTES


def test_maximal_session_result_size(report: Report) -> None:
    document = maximal_result()

    size = encoded_bytes(document)

    report.environment["session_result_maximal_bytes"] = size
    assert size > 1_048_576
