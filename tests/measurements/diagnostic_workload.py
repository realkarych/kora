from __future__ import annotations

import gc
import os
import sys
import threading
import time
import tracemalloc
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pydantic
import pydantic_core
from pydantic import BaseModel, ValidationError

from kora.cel import Diagnostic as CelDiagnostic
from kora.cel import check
from kora.ir import Diagnostic
from kora.ir.diagnostics import from_cel
from tests.measurements.harness import current_rss_bytes, open_descriptors

CYCLES: int = 1000
INTERVAL: int = 100
POINTS: int = 11
WIDTH: int = 8
SUMMARY_BYTES: int = 4096
MAX_INTEGER: int = 9_007_199_254_740_991


@dataclass(frozen=True)
class Case:
    name: str
    action: Callable[[], object]
    accepted: bool = True


class Point(BaseModel):
    cycle: int
    heap: int | None
    peak: int | None
    tracer: int | None
    rss: int | None
    high_water: int | None
    descriptors: int
    threads: int
    os_threads: int | None


class Lifecycle(BaseModel):
    traced: bool
    seconds: float
    successes: int
    refusals: int
    points: list[Point]
    retaining_paths: list[str]
    fixtures: dict[str, dict[str, int]]
    python: str
    pydantic: str
    pydantic_core: str
    platform: str
    page_size: int
    pid: int
    revision: str
    run: str


def validate(document: dict[str, object]) -> Diagnostic:
    return Diagnostic.model_validate(document)


def round_trip(document: dict[str, object]) -> Diagnostic:
    return Diagnostic.model_validate_json(validate(document).model_dump_json())


def convert(exact: bool) -> Diagnostic:
    parsed = check("'😀' == x @")
    assert isinstance(parsed, CelDiagnostic)
    value = from_cel(parsed, path=("condition",), exact_expression_field=exact)
    assert value.offset == (9 if exact else None)
    return value


def fixtures() -> dict[str, dict[str, object]]:
    documents: dict[str, dict[str, object]] = {
        "minimal": {"code": "X", "message": ""},
        "root": {"code": "X", "message": "", "path": []},
    }
    for name, unit in (("ascii", "a"), ("non_bmp", "😀"), ("escaped", "\n")):
        width = len(unit.encode())
        documents[name] = {
            "code": "X" * 64,
            "message": unit * (1024 // width),
            "path": [
                str(index).zfill(4) + unit * (252 // width) for index in range(64)
            ],
            "location": {"file": unit * (1024 // width), "line": MAX_INTEGER},
            "offset": MAX_INTEGER,
            "node": unit * (256 // width),
            "hint": unit * (512 // width),
        }
    documents["integers"] = {**documents["ascii"], "path": [MAX_INTEGER] * 64}
    return documents


def cases() -> tuple[list[Case], dict[str, dict[str, int]]]:
    actions: list[Case] = []
    lengths: dict[str, dict[str, int]] = {}
    for name, document in fixtures().items():
        value = validate(document)
        wire = value.model_dump_json()
        lengths[name] = {
            "json_codepoints": len(wire),
            "json_utf8_bytes": len(wire.encode()),
            "message_codepoints": len(value.message),
            "message_utf8_bytes": len(value.message.encode()),
            "path_segments": len(value.path) if value.path is not None else 0,
        }
        actions.extend(
            [
                Case(f"{name}_construct", partial(validate, document)),
                Case(f"{name}_encode", value.model_dump_json),
                Case(f"{name}_parse", partial(Diagnostic.model_validate_json, wire)),
                Case(f"{name}_round_trip", partial(round_trip, document)),
            ]
        )
    actions.extend(
        [
            Case("cel_exact", partial(convert, True)),
            Case("cel_ancestor", partial(convert, False)),
        ]
    )
    invalid: dict[str, dict[str, object]] = {
        "code": {"code": "a\n"},
        "unknown": {"unknown": "value"},
        "nested_unknown": {"location": {"file": "a", "line": 1, "unknown": 1}},
        "message_over": {"message": "a" * 1025},
        "node_over": {"node": "😀" * 64 + "a"},
        "hint_over": {"hint": "a" * 513},
        "source_over": {"location": {"file": "a" * 1025, "line": 1}},
        "path_over": {"path": ["a"] * 65},
        "segment_over": {"path": ["a" * 257]},
        "surrogate": {"message": chr(0xD800)},
        "bool": {"path": [True]},
        "negative": {"path": [-1]},
        "overflow": {"path": [2**53]},
        "float": {"path": [1.0]},
        "line_zero": {"location": {"file": "a", "line": 0}},
        "absolute_source": {"location": {"file": "/private/a", "line": 1}},
        "large_text": {"message": "a" * 1_048_576},
        "large_path": {"path": ["a"] * 4096},
    }
    for name, fields in invalid.items():
        document = {"code": "X", "message": "Invalid value.", **fields}
        actions.append(Case(f"refuse_{name}", partial(validate, document), False))
    return actions, lengths


def exercise(case: Case) -> None:
    try:
        case.action()
    except ValidationError:
        assert not case.accepted, case.name
    else:
        assert case.accepted, case.name


def linux_status() -> tuple[int, int]:
    if sys.platform != "linux":
        return -1, -1
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
    )
    return int(status["VmHWM"].split()[0]) * 1024, int(status["Threads"])


def sample(storage: array[int], index: int, traced: bool) -> None:
    gc.collect()
    heap, peak = tracemalloc.get_traced_memory() if traced else (-1, -1)
    tracer = tracemalloc.get_tracemalloc_memory() if traced else -1
    rss = current_rss_bytes()
    high_water, os_threads = linux_status()
    values = (
        heap,
        peak,
        tracer,
        rss if rss is not None else -1,
        high_water,
        open_descriptors(),
        threading.active_count(),
        os_threads,
    )
    for column, value in enumerate(values):
        storage[index * WIDTH + column] = value


def retain_paths(baseline: tracemalloc.Snapshot, storage: bytearray) -> None:
    snapshot = tracemalloc.take_snapshot()
    positive = [
        item
        for item in snapshot.compare_to(baseline, "traceback")
        if item.size_diff > 0
    ]
    summary = "\n".join(
        f"{item.size_diff} bytes: {item.traceback}" for item in positive[:8]
    )
    encoded = summary.encode()[:SUMMARY_BYTES]
    memoryview(storage)[: len(encoded)] = encoded


def point(storage: array[int], index: int) -> Point:
    values = [int(storage[index * WIDTH + column]) for column in range(WIDTH)]
    return Point(
        cycle=index * INTERVAL,
        heap=values[0] if values[0] >= 0 else None,
        peak=values[1] if values[1] >= 0 else None,
        tracer=values[2] if values[2] >= 0 else None,
        rss=values[3] if values[3] >= 0 else None,
        high_water=values[4] if values[4] >= 0 else None,
        descriptors=values[5],
        threads=values[6],
        os_threads=values[7] if values[7] >= 0 else None,
    )


def run(traced: bool, revision: str, identity: str) -> Lifecycle:
    actions, lengths = cases()
    for _ in range(3):
        for action in actions:
            exercise(action)
    storage = array("q", [0] * (POINTS * WIDTH))
    summaries = [bytearray(SUMMARY_BYTES), bytearray(SUMMARY_BYTES)]
    baseline: tracemalloc.Snapshot | None = None
    if traced:
        tracemalloc.start()
        baseline = tracemalloc.take_snapshot()
    sample(storage, 0, traced)

    start = time.perf_counter()
    for cycle in range(1, CYCLES + 1):
        for action in actions:
            exercise(action)
        if cycle % INTERVAL == 0:
            sample(storage, cycle // INTERVAL, traced)
        if cycle in (500, 1000) and baseline is not None:
            retain_paths(baseline, summaries[cycle // 500 - 1])
    elapsed = time.perf_counter() - start
    if traced:
        tracemalloc.stop()

    return Lifecycle(
        traced=traced,
        seconds=elapsed,
        successes=CYCLES * sum(action.accepted for action in actions),
        refusals=CYCLES * sum(not action.accepted for action in actions),
        points=[point(storage, index) for index in range(POINTS)],
        retaining_paths=[
            bytes(value).rstrip(b"\0").decode("utf-8") for value in summaries
        ],
        fixtures=lengths,
        python=sys.version,
        pydantic=pydantic.__version__,
        pydantic_core=pydantic_core.__version__,
        platform=sys.platform,
        page_size=os.sysconf("SC_PAGE_SIZE"),
        pid=os.getpid(),
        revision=revision,
        run=identity,
    )


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in {"traced", "untraced"}:
        raise ValueError("expected tracing mode, revision and run identity")
    result = run(sys.argv[1] == "traced", sys.argv[2], sys.argv[3])
    sys.stdout.write(result.model_dump_json() + "\n")
