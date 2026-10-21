import json
import time
from collections.abc import Iterator
from typing import TypedDict

import pytest

from tests.cel_eval.children import ROOT, full_path, run_child
from tests.measurements.harness import Report, Sample, new_report, write_report

pytestmark = pytest.mark.measurement
REPEATS: int = 3


class Payload(TypedDict):
    mode: str
    seconds: float
    points: list[dict[str, int | None]]
    timings: list[float]
    retaining_paths: list[str]
    stack_restored: bool
    persistent_workers: int
    terminal_workers: int | None


@pytest.fixture(scope="module")
def report() -> Iterator[Report]:
    created = new_report()
    created.environment.update(warmup=20, cycles=200, independent_processes=REPEATS)
    yield created
    write_report(created)


def test_cel_evaluator_resource_lifetime(report: Report) -> None:
    started = time.perf_counter()
    for mode in ("native", "lifetime", "idle", "busy"):
        for repeat in range(REPEATS):
            code = (
                "from tests.measurements.cel_workload import run\n"
                f"print(run({mode!r}))\n"
            )
            before = time.perf_counter()

            result = run_child(code, full_path(ROOT))
            elapsed = time.perf_counter() - before

            assert result.code == 0, result.stderr
            assert result.stderr == ""
            payload: Payload = json.loads(result.stdout)
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
                assert point["handles"] == 0
                assert point["outstanding"] == 0
                assert point["payload_bytes"] == 0
                assert point["tasks"] == 1
                entries, characters = point["cache_entries"], point["cache_characters"]
                assert entries is not None
                assert characters is not None
                assert entries <= (128 if mode == "native" else 8)
                assert characters <= (131072 if mode == "native" else 64)
            report.environment[f"{mode}_{repeat}_retainers"] = "\n".join(
                payload["retaining_paths"]
            )
            report.gates[f"{mode}_{repeat}_stack"] = payload["stack_restored"]
            report.gates[f"{mode}_{repeat}_worker"] = payload["persistent_workers"] == 1
            assert payload["stack_restored"]
            assert payload["persistent_workers"] == 1
            if mode == "lifetime":
                assert payload["terminal_workers"] == 0
            if payload["points"]:
                points = payload["points"]
                for resource in ("descriptors", "threads", "os_threads", "tasks"):
                    assert len({point[resource] for point in points}) == 1
    report.durations["whole_step"] = time.perf_counter() - started


@pytest.mark.parametrize("sink", ["draining", "blocked", "broken"])
@pytest.mark.parametrize("level", ["CRITICAL", "INFO", "DEBUG"])
def test_cel_diagnostics_do_not_delay_exit(
    report: Report, sink: str, level: str
) -> None:
    code = (
        "from tests.measurements.cel_sink_workload import run\n"
        f"print(run({sink!r}, {level!r}))\n"
    )
    started = time.perf_counter()

    result = run_child(code, full_path(ROOT))

    assert result.code == 0, result.stderr
    assert result.stderr == ""
    payload: dict[str, float | bool | int] = json.loads(result.stdout)
    report.samples.append(
        Sample(
            f"sink_{sink}_{level}", 0, 200, time.perf_counter() - started, dict(payload)
        )
    )
    if sink == "blocked" and level != "CRITICAL":
        assert payload["dropped"] > 0
    if sink == "broken" and level != "CRITICAL":
        assert payload["sink_failed"]


@pytest.mark.parametrize("sink", ["draining", "blocked", "broken"])
def test_cel_process_exits_with_native_work_in_flight(
    report: Report, sink: str
) -> None:
    code = (
        "from tests.measurements.cel_sink_workload import run\n"
        f"print(run({sink!r}, 'DEBUG', True))\n"
    )
    started = time.perf_counter()

    result = run_child(code, full_path(ROOT))

    assert result.code == 0, result.stderr
    report.samples.append(
        Sample(f"inflight_exit_{sink}", 0, 1, time.perf_counter() - started)
    )
