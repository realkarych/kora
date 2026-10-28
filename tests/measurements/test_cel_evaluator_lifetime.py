import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TypedDict

import pytest

from tests.cel_eval.children import ROOT, ChildResult, full_path, run_child
from tests.measurements.cel_native_pair import run_native
from tests.measurements.harness import Report, Sample, new_report, write_report

pytestmark = pytest.mark.measurement
REPEATS: int = 3


@dataclass(frozen=True)
class ResourceBudget:
    heap_growth: int
    late_heap_growth: int
    peak_heap: int
    rss_growth: int
    late_rss_span: int


SINK_BUDGET: ResourceBudget = ResourceBudget(32768, 16384, 196608, 524288, 262144)
RESOURCE_BUDGETS: dict[str, ResourceBudget] = {
    "native": ResourceBudget(73728, 20480, 327680, 655360, 327680),
    "lifetime": ResourceBudget(24576, 12288, 720896, 262144, 196608),
    "loops": ResourceBudget(16384, 8192, 65536, 32768, 24576),
}
HOP_P95_SECONDS: dict[str, float] = {"idle": 0.000175, "busy": 0.0016}


class Payload(TypedDict):
    mode: str
    seconds: float
    points: list[dict[str, int | None]]
    timings: list[float]
    retaining_paths: list[str]
    stack_restored: bool
    persistent_workers: int
    terminal_workers: int | None
    startup_hash: int
    traced: bool


class SinkPayload(TypedDict):
    stats: dict[str, float | bool | int]
    points: list[dict[str, int | None]]
    retaining_paths: list[str]


def gate(report: Report, name: str, passed: bool) -> None:
    report.gates[name] = passed
    assert passed, name


def diagnostic(report: Report, name: str, passed: bool, measured: float) -> None:
    report.environment[f"diagnostic_{name}"] = passed
    report.environment[f"diagnostic_{name}_measured"] = measured


def bounded(
    report: Report, name: str, measured: int, limit: int, blocking: bool
) -> None:
    if blocking:
        gate(report, name, measured <= limit)
    else:
        diagnostic(report, name, measured <= limit, measured)


def spans(points: list[dict[str, int | None]], resource: str) -> tuple[int, int]:
    measured = [point[resource] for point in points]
    values = [value for value in measured if value is not None]
    assert len(values) == len(points)
    late = values[9:]
    return max(values) - min(values), max(late) - min(late)


def resource_gates(
    report: Report,
    name: str,
    points: list[dict[str, int | None]],
    budget: ResourceBudget,
    *,
    native_traced: bool = False,
    native_untraced: bool = False,
    rss_gated: bool = False,
) -> None:
    for resource in ("descriptors", "threads", "os_threads", "tasks"):
        gate(report, f"{name}_{resource}", len({p[resource] for p in points}) == 1)
    for resource, whole, late in (
        ("heap", budget.heap_growth, budget.late_heap_growth),
        ("rss", budget.rss_growth, budget.late_rss_span),
    ):
        values = [point[resource] for point in points]
        if resource == "heap" and native_untraced:
            assert all(value is None for value in values)
            continue
        if values[0] is None:
            assert report.platform != "linux"
            assert all(value is None for value in values)
            continue
        growth, change = spans(points, resource)
        blocking = resource == "heap" or rss_gated
        bounded(report, f"{name}_{resource}_growth", growth, whole, blocking)
        if resource != "rss" or not native_traced:
            bounded(report, f"{name}_{resource}_late_growth", change, late, blocking)
    peaks = [p["peak"] for p in points if p["peak"] is not None]
    if native_untraced:
        assert not peaks
    else:
        gate(report, f"{name}_peak_heap", max(peaks) <= budget.peak_heap)
    image_peaks = [
        p["image_peak_rss"] for p in points if p["image_peak_rss"] is not None
    ]
    if report.platform == "linux":
        assert len(image_peaks) == len(points)
    if image_peaks:
        gate(report, f"{name}_image_peak_rss", max(image_peaks) <= 134217728)


def sink_gates(report: Report, name: str, payload: SinkPayload, blocked: bool) -> None:
    stats = payload["stats"]
    gate(report, f"{name}_shutdown", stats["shutdown_seconds"] <= 0.05)
    gate(report, f"{name}_writer", stats["writer_alive_at_exit"] == blocked)
    gate(report, f"{name}_drained", stats["drained"] == (not blocked))
    if blocked:
        gate(report, f"{name}_pipe_full", stats["filled_pipe_bytes"] > 0)


@pytest.fixture(scope="module")
def report() -> Iterator[Report]:
    started = time.perf_counter()
    created = new_report()
    created.environment.update(warmup=20, cycles=200, independent_processes=REPEATS)
    yield created
    created.durations["whole_suite"] = time.perf_counter() - started
    created.gates["whole_suite_budget"] = created.durations["whole_suite"] <= 180
    write_report(created)
    assert created.gates["whole_suite_budget"]


def test_cel_evaluator_resource_lifetime(report: Report) -> None:
    started = time.perf_counter()
    native_results: dict[tuple[bool, int], Payload] = {}
    native_children: dict[tuple[bool, int], tuple[ChildResult, float]] = {}
    for traced, repeat in (
        (True, 0),
        (False, 0),
        (False, 1),
        (True, 1),
        (True, 2),
        (False, 2),
    ):
        before = time.perf_counter()
        result = run_native(traced, repeat)
        elapsed = time.perf_counter() - before
        name = f"native_{'traced' if traced else 'untraced'}_{repeat}"
        report.environment[f"{name}_stdout"] = result.stdout
        report.environment[f"{name}_stderr"] = result.stderr
        report.environment[f"{name}_exit"] = result.code
        report.samples.append(Sample(name + "_process_exit", repeat, 1, elapsed))
        native_children[traced, repeat] = result, elapsed
    assert all(
        child.code == 0 and child.stderr == "" for child, _ in native_children.values()
    )
    for key, (child, _) in native_children.items():
        native_results[key] = json.loads(child.stdout)
    for repeat in range(REPEATS):
        traced_payload = native_results[True, repeat]
        untraced_payload = native_results[False, repeat]
        assert traced_payload["startup_hash"] == untraced_payload["startup_hash"]
        for payload in (traced_payload, untraced_payload):
            assert len(payload["points"]) == 20
            assert payload["persistent_workers"] == 1
            assert payload["stack_restored"]
            for point in payload["points"]:
                assert point["handles"] == 0
                assert point["outstanding"] == 0
                assert point["payload_bytes"] == 0
                assert point["tasks"] == 1
                entries, characters = point["cache_entries"], point["cache_characters"]
                assert entries is not None
                assert entries <= 128
                assert characters is not None
                assert characters <= 131072
                assert (point["heap"] is not None) == payload["traced"]
                assert (point["peak"] is not None) == payload["traced"]
            for resource in ("descriptors", "threads", "os_threads", "tasks"):
                assert len({point[resource] for point in payload["points"]}) == 1
        for point in untraced_payload["points"]:
            cycle = point["cycle"]
            assert cycle is not None
            report.samples.append(
                Sample(
                    "native_untraced_resources",
                    repeat,
                    cycle,
                    untraced_payload["seconds"],
                    dict(point),
                )
            )
        resource_gates(
            report,
            f"native_untraced_{repeat}",
            untraced_payload["points"],
            RESOURCE_BUDGETS["native"],
            native_untraced=True,
            rss_gated=True,
        )
    report.environment["native_late_rss_basis"] = "untraced paired workload"
    for mode in ("native", "lifetime", "loops", "idle", "busy"):
        for repeat in range(REPEATS):
            code = (
                "from tests.measurements.cel_workload import run\n"
                f"print(run({mode!r}))\n"
            )
            before = time.perf_counter()

            if mode == "native":
                payload = native_results[True, repeat]
                elapsed = native_children[True, repeat][1]
            else:
                result = run_child(code, full_path(ROOT))
                elapsed = time.perf_counter() - before
                assert result.code == 0, result.stderr
                assert result.stderr == ""
                payload = json.loads(result.stdout)
            report.samples.append(Sample(mode + "_process_exit", repeat, 1, elapsed))
            for index, seconds in enumerate(payload["timings"]):
                report.samples.append(Sample(mode, repeat * 200 + index, 1, seconds))
            for point in payload["points"]:
                cycle = point["cycle"]
                assert cycle is not None
                report.samples.append(
                    Sample(
                        mode + "_resources",
                        repeat,
                        cycle,
                        payload["seconds"],
                        dict(point),
                    )
                )
            report.environment[f"{mode}_{repeat}_retainers"] = "\n".join(
                payload["retaining_paths"]
            )
            for point in payload["points"]:
                assert point["handles"] == 0
                assert point["outstanding"] == 0
                assert point["payload_bytes"] == 0
                assert point["tasks"] == (0 if mode == "loops" else 1)
                entries, characters = point["cache_entries"], point["cache_characters"]
                assert entries is not None
                assert characters is not None
                assert entries <= (128 if mode == "native" else 8)
                assert characters <= (131072 if mode == "native" else 64)
            report.gates[f"{mode}_{repeat}_stack"] = payload["stack_restored"]
            report.gates[f"{mode}_{repeat}_worker"] = payload["persistent_workers"] == 1
            assert payload["stack_restored"]
            assert payload["persistent_workers"] == 1
            if mode == "lifetime":
                assert payload["terminal_workers"] == 0
            if payload["points"]:
                points = payload["points"]
                resource_gates(
                    report,
                    f"{mode}_{repeat}",
                    points,
                    RESOURCE_BUDGETS[mode],
                    native_traced=mode == "native",
                    rss_gated=mode == "native",
                )
            if payload["timings"]:
                timings = sorted(payload["timings"])
                assert len(timings) == 200
                diagnostic(
                    report,
                    f"{mode}_{repeat}_hop_p95",
                    timings[189] <= HOP_P95_SECONDS[mode],
                    timings[189],
                )
    report.durations["whole_step"] = time.perf_counter() - started
    gate(report, "whole_step_budget", report.durations["whole_step"] <= 150)


@pytest.mark.parametrize("repeat", range(REPEATS))
@pytest.mark.parametrize("sink", ["draining", "blocked", "broken"])
@pytest.mark.parametrize("level", ["CRITICAL", "INFO", "DEBUG"])
def test_cel_diagnostics_do_not_delay_exit(
    report: Report, sink: str, level: str, repeat: int
) -> None:
    code = (
        "from tests.measurements.cel_sink_workload import run\n"
        f"print(run({sink!r}, {level!r}))\n"
    )
    started = time.perf_counter()

    result = run_child(code, full_path(ROOT))

    assert result.code == 0, result.stderr
    assert result.stderr == ""
    payload: SinkPayload = json.loads(result.stdout)
    elapsed = time.perf_counter() - started
    name = f"sink_{sink}_{level}_{repeat}"
    report.samples.append(
        Sample(
            f"sink_{sink}_{level}",
            repeat,
            200,
            elapsed,
            dict(payload["stats"]),
        )
    )
    for point in payload["points"]:
        cycle = point["cycle"]
        assert cycle is not None
        report.samples.append(
            Sample(f"sink_resources_{sink}_{level}", repeat, cycle, 0, dict(point))
        )
    report.environment[f"sink_{sink}_{level}_{repeat}_retainers"] = "\n".join(
        payload["retaining_paths"]
    )
    gate(report, f"{name}_exit", elapsed <= 1.0)
    sink_gates(report, name, payload, sink == "blocked" and level != "CRITICAL")
    resource_gates(report, name, payload["points"], SINK_BUDGET)
    for point in payload["points"]:
        assert point["outstanding"] == 0
        assert point["payload_bytes"] == 0
        assert point["handles"] == 0
        assert point["lifecycle_reports"] == 0
        assert point["tasks"] == 1
        queue_records, queue_bytes = point["queue_records"], point["queue_bytes"]
        assert queue_records is not None
        assert queue_bytes is not None
        assert queue_records <= 16
        assert queue_bytes <= 65536
    if sink == "blocked" and level != "CRITICAL":
        assert payload["stats"]["dropped"] > 0
    if sink == "broken" and level != "CRITICAL":
        assert payload["stats"]["sink_failed"]


@pytest.mark.parametrize("repeat", range(REPEATS))
@pytest.mark.parametrize("sink", ["draining", "blocked", "broken"])
def test_cel_process_exits_with_evaluator_work_in_flight(
    report: Report, sink: str, repeat: int
) -> None:
    code = (
        "from tests.measurements.cel_sink_workload import run\n"
        f"print(run({sink!r}, 'DEBUG', True))\n"
    )
    started = time.perf_counter()

    result = run_child(code, full_path(ROOT))

    assert result.code == 0, result.stderr
    assert result.stderr == ""
    payload: SinkPayload = json.loads(result.stdout)
    elapsed = time.perf_counter() - started
    name = f"inflight_exit_{sink}_{repeat}"
    report.samples.append(
        Sample(f"inflight_exit_{sink}", repeat, 1, elapsed, dict(payload["stats"]))
    )
    gate(report, f"{name}_exit", elapsed <= 1.0)
    sink_gates(report, name, payload, sink == "blocked")


def test_cel_lifetime_heap_gate_detects_a_retained_leak() -> None:
    code = (
        "from tests.measurements.cel_workload import run\nprint(run('leaking_loops'))\n"
    )
    budget = RESOURCE_BUDGETS["loops"]

    result = run_child(code, full_path(ROOT))

    assert result.code == 0, result.stderr
    payload: Payload = json.loads(result.stdout)
    growth, late = spans(payload["points"], "heap")
    assert payload["mode"] == "leaking_loops"
    assert growth > budget.heap_growth
    assert late > budget.late_heap_growth
