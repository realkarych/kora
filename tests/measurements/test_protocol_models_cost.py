import gc
import json
import statistics
import time
import tracemalloc
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

import kora.conformance.capture as capture_module
from kora.conformance.capture import Capture, captured
from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.ir.json import JsonObject, JsonValue
from kora.ir.vcs_ref_json import canonical_bytes
from kora.observability import LoggingSettings, ProcessDiagnostics
from kora.plugins.policy.allow_deny import (
    DECISION_FUEL,
    PREPARED_ENTRIES,
    AllowDenyPolicy,
)
from kora.plugins.policy.allow_deny.patterns import MAX_PATTERNS
from kora.protocols.identity import GrantBinding
from kora.protocols.secret_value import SecretScope
from tests.measurements.agent_session_artifact_type_workload import (
    collect as collect_agent_session_artifact_type,
)
from tests.measurements.artifact_type_workload import collect as collect_artifact_type
from tests.measurements.command_result_artifact_type_workload import (
    collect as collect_command_result_artifact_type,
)
from tests.measurements.harness import (
    Report,
    Sample,
    current_rss_bytes,
    open_descriptors,
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
from tests.measurements.local_codec_workload import CODEC_PROBE, HOSTILE_ITEMS
from tests.measurements.policy_workload import (
    DECIDED_ROWS,
    FLOOR_SHARE,
    FLOORED,
    REALISTIC,
    ROWS,
    PolicyCall,
    composite_variant,
    decide,
    spend,
)
from tests.measurements.runtime_workload import (
    BODY_CAP_BYTES,
    CONTROL_FRAME_BYTES,
    MAXIMAL,
    Action,
    chain_rows,
    encoded,
    maximal_workload,
    page_rows,
)
from tests.measurements.structured_output_artifact_type_workload import (
    collect as collect_structured_output_artifact_type,
)
from tests.support.heap import recurring_lines, snapshot, top_growth
from tests.support.observability import run_python

pytestmark = pytest.mark.measurement


def test_artifact_type_provider_cost(report: Report) -> None:
    collect_artifact_type(report)


def test_command_result_artifact_type_provider_cost(report: Report) -> None:
    collect_command_result_artifact_type(report)


def test_agent_session_artifact_type_provider_cost(report: Report) -> None:
    collect_agent_session_artifact_type(report)


def test_structured_output_artifact_type_provider_cost(report: Report) -> None:
    collect_structured_output_artifact_type(report)


INDEPENDENT_RUNS: int = 3
MEASURED: int = 30
WARM_UP: int = 5
GRANTS: int = 64
REGISTRATION_CYCLES: int = 3
HEAD_BYTES: int = 8
TRACED_CYCLES: int = 2
RETAINED_FLOOR_BYTES: int = 4096
HEAP_GROWTH_LIMIT_BYTES: int = 262_144
ROW_WARM_UP: int = 3
ROW_MEASURED: int = 30
ROW_DENSE_MEASURED: int = 10
ROW_RETAINED_LIMIT_BYTES: int = 65_536
POLICY_CEILING_SECONDS: float = 0.1
POLICY_PEAK_LIMIT_BYTES: int = 16 * 1024 * 1024
POLICY_CACHE_ENTRY_LIMIT_BYTES: int = 5 * 1024 * 1024
CAPTURE_MODULE: str = capture_module.__file__ or ""
EXECUTION_TRACED_CYCLES: int = 4
PAGE_ROWS: dict[str, tuple[Action, int]] = page_rows()
CHAIN_ROWS: dict[str, tuple[Action, int]] = chain_rows()
IMPORT_PROBE: str = (
    "import json, sys, time\n"
    "start = time.perf_counter()\n"
    "import kora.protocols.common, kora.protocols.identity, kora.protocols.secrets\n"
    "import kora.protocols.deadlines, kora.protocols.evidence, kora.protocols.report\n"
    "import kora.protocols.history, kora.protocols.vcs, kora.protocols.runtime\n"
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
    return ROW_MEASURED if "mixed_small" in workload else ROW_DENSE_MEASURED


@pytest.mark.parametrize("workload", WORKLOADS)
def test_harness_model_validation_cost(report: Report, workload: str) -> None:
    action, count = build(workload)
    report.environment[f"{workload}_rss_start_bytes"] = current_rss_bytes()
    process_peak_before = peak_rss_bytes()
    for _ in range(ROW_WARM_UP):
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
    if workload == "session_result_maximal_json":
        report.environment["session_result_maximal_bytes"] = encoded_bytes(
            maximal_result()
        )
    report.gates[f"{workload}_memory_observed"] = True
    report.diagnostic(
        f"{workload}_retained_bytes",
        retained,
        ROW_RETAINED_LIMIT_BYTES,
        allow_equal=False,
    )
    report.diagnostic(
        f"{workload}_recurring_bytes",
        recurring,
        ROW_RETAINED_LIMIT_BYTES,
        allow_equal=False,
    )


def expected_outcome(row: str) -> str:
    return "decided" if row in REALISTIC or row in DECIDED_ROWS else "exhausted"


def cold_decisions(subject: PolicyCall, capture: Capture) -> list[float]:
    seconds: list[float] = []
    for _ in range(ROW_DENSE_MEASURED):
        provider = AllowDenyPolicy()
        start = time.perf_counter()
        decide(subject, capture, provider)
        seconds.append(time.perf_counter() - start)
    return seconds


def timed_decisions(
    subject: PolicyCall, capture: Capture, provider: AllowDenyPolicy
) -> list[float]:
    for _ in range(ROW_WARM_UP):
        decide(subject, capture, provider)
    seconds: list[float] = []
    for _ in range(ROW_DENSE_MEASURED):
        start = time.perf_counter()
        decide(subject, capture, provider)
        seconds.append(time.perf_counter() - start)
    return seconds


def outside_capture(snapshot: tracemalloc.Snapshot) -> int:
    kept = snapshot.filter_traces([tracemalloc.Filter(False, CAPTURE_MODULE)])
    return sum(statistic.size for statistic in kept.statistics("filename"))


def traced_decision(
    subject: PolicyCall, capture: Capture, provider: AllowDenyPolicy
) -> tuple[int, int]:
    gc.collect()
    tracemalloc.start()
    baseline = outside_capture(tracemalloc.take_snapshot())
    decide(subject, capture, provider)
    gc.collect()
    peak = tracemalloc.get_traced_memory()[1]
    retained = outside_capture(tracemalloc.take_snapshot()) - baseline
    tracemalloc.stop()
    return retained, peak


@pytest.mark.parametrize("row", sorted(ROWS | REALISTIC))
def test_policy_decision_cost(report: Report, row: str, tmp_path: Path) -> None:
    subject = (ROWS | REALISTIC)[row](tmp_path)
    descriptors = open_descriptors()
    spent = spend(subject, DECISION_FUEL)
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), "policy", utc_now
    ) as capture:
        cold = cold_decisions(subject, capture)
        _, cold_peak = traced_decision(subject, capture, AllowDenyPolicy())
        provider = AllowDenyPolicy()
        seconds = timed_decisions(subject, capture, provider)
        retained, peak = traced_decision(subject, capture, provider)

    for repeat, value in enumerate(seconds):
        report.samples.append(Sample(row, repeat, 1, value))
    for repeat, value in enumerate(cold):
        report.samples.append(Sample(f"{row}_cold", repeat, 1, value))
    report.gates[f"{row}_cold_within_ceiling"] = max(cold) < POLICY_CEILING_SECONDS
    report.environment[f"{row}_outcome"] = spent.outcome
    report.environment[f"{row}_fuel_spent"] = spent.units
    report.environment[f"{row}_peak_bytes"] = peak
    report.environment[f"{row}_cold_peak_bytes"] = cold_peak
    report.environment[f"{row}_retained_bytes"] = retained
    report.environment[f"{row}_ns_per_unit"] = (
        statistics.median(seconds) * 1e9 / max(spent.units, 1)
    )
    report.gates[f"{row}_within_ceiling"] = max(seconds) < POLICY_CEILING_SECONDS
    assert open_descriptors() == descriptors
    report.diagnostic(
        f"{row}_retained_bytes", retained, ROW_RETAINED_LIMIT_BYTES, allow_equal=False
    )
    assert peak < POLICY_PEAK_LIMIT_BYTES
    assert cold_peak < POLICY_PEAK_LIMIT_BYTES
    assert spent.outcome == expected_outcome(row)
    if row in FLOORED:
        assert spent.units <= DECISION_FUEL * FLOOR_SHARE


def test_policy_validate_cost(report: Report) -> None:
    malformed: list[JsonValue] = ["("] * MAX_PATTERNS
    document: JsonObject = {"allow": malformed, "deny": malformed}
    provider = AllowDenyPolicy()
    for _ in range(ROW_WARM_UP):
        provider.validate(document)

    seconds: list[float] = []
    found: list[object] = []
    for repeat in range(ROW_DENSE_MEASURED):
        start = time.perf_counter()
        found = list(provider.validate(document))
        seconds.append(time.perf_counter() - start)
        report.samples.append(Sample("policy_validate", repeat, 1, seconds[-1]))

    report.gates["policy_validate_within_ceiling"] = (
        max(seconds) < POLICY_CEILING_SECONDS
    )
    assert len(found) == 2 * MAX_PATTERNS


def test_policy_configuration_cache_retention(report: Report, tmp_path: Path) -> None:
    gc.collect()
    tracemalloc.start()
    baseline = outside_capture(tracemalloc.take_snapshot())
    provider = AllowDenyPolicy()
    entry_sizes: list[int] = []
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), "policy", utc_now
    ) as capture:
        for index in range(PREPARED_ENTRIES + 2):
            before = outside_capture(tracemalloc.take_snapshot())
            decide(
                composite_variant(tmp_path / f"root{index}", index), capture, provider
            )
            gc.collect()
            entry_sizes.append(outside_capture(tracemalloc.take_snapshot()) - before)
        held = outside_capture(tracemalloc.take_snapshot()) - baseline
        entries = len(provider.configurations)
        del provider
    gc.collect()
    released = outside_capture(tracemalloc.take_snapshot()) - baseline
    tracemalloc.stop()

    report.environment["policy_cache_held_bytes"] = held
    report.environment["policy_cache_entry_bytes"] = max(entry_sizes[:PREPARED_ENTRIES])
    report.environment["policy_cache_released_bytes"] = released
    assert entries == PREPARED_ENTRIES
    assert max(entry_sizes[:PREPARED_ENTRIES]) <= POLICY_CACHE_ENTRY_LIMIT_BYTES
    assert held <= PREPARED_ENTRIES * POLICY_CACHE_ENTRY_LIMIT_BYTES
    report.diagnostic(
        "policy_cache_released_bytes",
        released,
        ROW_RETAINED_LIMIT_BYTES,
        allow_equal=False,
    )


def measure_execution_row(report: Report, row: str, action: Action, count: int) -> None:
    report.environment[f"{row}_rss_start_bytes"] = current_rss_bytes()
    process_peak_before = peak_rss_bytes()
    for _ in range(ROW_WARM_UP):
        action()
    for repeat in range(ROW_DENSE_MEASURED):
        start = time.perf_counter()
        action()
        report.samples.append(Sample(row, repeat, count, time.perf_counter() - start))
    tracemalloc.start()
    before = snapshot()
    for _ in range(EXECUTION_TRACED_CYCLES // 2):
        action()
    middle = snapshot()
    for _ in range(EXECUTION_TRACED_CYCLES - EXECUTION_TRACED_CYCLES // 2):
        action()
    after = snapshot()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    report.environment[f"{row}_rss_end_bytes"] = current_rss_bytes()
    report.environment[f"{row}_process_peak_rss_bytes"] = peak_rss_bytes()
    report.environment[f"{row}_process_peak_rss_growth_bytes"] = (
        peak_rss_bytes() - process_peak_before
    )

    retained = sum(stat.size_diff for stat in after.compare_to(before, "lineno"))
    lines = recurring_lines(before, middle, after, EXECUTION_TRACED_CYCLES)
    recurring = sum(stat.size_diff for stat in lines)
    report.environment[f"{row}_peak_bytes"] = peak
    report.environment[f"{row}_retained_bytes"] = retained
    report.environment[f"{row}_recurring_growth_bytes"] = recurring
    report.environment[f"{row}_top_growth"] = "; ".join(top_growth(lines, 3))
    report.gates[f"{row}_memory_observed"] = True
    report.diagnostic(
        f"{row}_retained_bytes", retained, ROW_RETAINED_LIMIT_BYTES, allow_equal=False
    )
    report.diagnostic(
        f"{row}_recurring_bytes", recurring, ROW_RETAINED_LIMIT_BYTES, allow_equal=False
    )


@pytest.mark.parametrize("row", sorted(MAXIMAL))
def test_maximal_execution_model_sizes(report: Report, row: str) -> None:
    model, build_document = MAXIMAL[row]
    payload = encoded(build_document())

    def action() -> object:
        return model.model_validate_json(payload)

    measure_execution_row(report, f"maximal_{row}", action, 1)

    report.environment[f"maximal_{row}_bytes"] = len(payload)
    report.environment[f"maximal_{row}_canonical_bytes"] = len(
        canonical_bytes(json.loads(payload))
    )
    report.environment[f"maximal_{row}_within_frame"] = (
        len(payload) <= CONTROL_FRAME_BYTES
    )
    report.environment[f"maximal_{row}_within_body_cap"] = (
        len(payload) <= BODY_CAP_BYTES
    )


@pytest.mark.parametrize("row", sorted(PAGE_ROWS))
def test_history_page_cost(report: Report, row: str) -> None:
    action, count = PAGE_ROWS[row]

    measure_execution_row(report, row, action, count)


@pytest.mark.parametrize("row", sorted(CHAIN_ROWS))
def test_hash_chain_cost(report: Report, row: str) -> None:
    action, count = CHAIN_ROWS[row]

    measure_execution_row(report, row, action, count)


@pytest.mark.parametrize(
    ("mode", "shape"),
    [("rss", "maximal"), ("traced", "maximal"), ("rss", "hostile")],
)
def test_local_codec_peak(
    report: Report, mode: str, shape: str, tmp_path: Path
) -> None:
    if shape == "hostile":
        payload = b"[" + b"{}," * HOSTILE_ITEMS + b"{}]"
    else:
        document = json.loads(encoded(maximal_workload()))
        tracemalloc.start()
        payload = canonical_bytes(document)
        report.environment["local_codec_canonical_encoding_traced_peak_bytes"] = (
            tracemalloc.get_traced_memory()[1]
        )
        tracemalloc.stop()
    source = tmp_path / "workload.json"
    source.write_bytes(payload)

    for run in range(INDEPENDENT_RUNS):
        result = run_python(["-I", "-c", CODEC_PROBE, str(source), mode], {})

        assert result.code == 0, result.stderr
        measured: dict[str, float] = json.loads(result.stdout)
        assert measured["refused"] == (shape == "hostile")
        seconds = measured["receive_cpu_seconds"] + measured["decode_cpu_seconds"]
        report.samples.append(
            Sample(f"local_codec_{shape}_{mode}", run, 1, seconds, dict(measured))
        )
