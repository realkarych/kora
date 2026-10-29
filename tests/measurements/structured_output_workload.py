import gc
import os
import sys
import time
import tracemalloc
from array import array
from collections.abc import Callable
from functools import partial

import pydantic
import pydantic_core

from kora.ir import (
    StructuredOutputValidationError,
    canonical_structured_output,
    parse_structured_output,
    parse_structured_output_json,
    structured_output_digest,
    summarize_structured_output,
)
from tests.ir.structured_output_cases import (
    exact_string,
    forged,
    hostile_inputs,
    nested,
)
from tests.measurements.diagnostic_workload import (
    SUMMARY_BYTES,
    WIDTH,
    Case,
    Lifecycle,
    point,
    retain_paths,
    sample,
)

BATCHES: int = 20
INTERVAL: int = 25
SUCCESSES: int = 248
REFUSALS: int = 252


def exercise(case: Case) -> None:
    try:
        case.action()
    except StructuredOutputValidationError:
        assert not case.accepted, case.name
    else:
        assert case.accepted, case.name


def near_cap_float(overflow: bool = False) -> bytes:
    suffix = b"e999999" if overflow else b"e-1"
    return b"1." + b"0" * (1_048_576 - 2 - len(suffix)) + suffix


def cases() -> tuple[list[Case], dict[str, dict[str, int]]]:
    actions: list[Case] = []
    manifest: dict[str, dict[str, int]] = {}
    for size in (1024, 65_536, 1_048_575, 1_048_576):
        candidate = exact_string(size)
        raw = ('"' + candidate + '"').encode()
        value = parse_structured_output(candidate)
        phases: dict[str, Callable[[], object]] = {
            "native": partial(parse_structured_output, candidate),
            "raw": partial(parse_structured_output_json, raw),
            "canonical": partial(canonical_structured_output, value),
            "digest": partial(structured_output_digest, value),
            "summary": partial(summarize_structured_output, value),
        }
        for phase, action in phases.items():
            name = f"structured_{phase}_{size}"
            actions.append(Case(name, action))
            manifest[name] = {"raw_bytes": size, "canonical_bytes": size}
    native: list[tuple[str, object, bool]] = [
        ("integer_at", (1 << 14_000) - 1, True),
        ("integer_over", 1 << 14_000, False),
        ("depth_at", nested(16), True),
        ("depth_over", nested(17), False),
        ("canonical_over", exact_string(1_048_577), False),
        ("nul", "\0" * 1024, True),
        ("unicode", "😀" * 16_384, True),
    ]
    native.extend(
        (name, factory(), False) for name, factory in hostile_inputs().items()
    )
    for name, candidate, accepted in native:
        identity = f"structured_{name}"
        actions.append(
            Case(identity, partial(parse_structured_output, candidate), accepted)
        )
        manifest[identity] = {"accepted": int(accepted)}
    raw_cases: list[tuple[str, bytes, bool]] = [
        ("float_near_cap", near_cap_float(), True),
        ("float_near_cap_overflow", near_cap_float(True), False),
        ("float_long_exponent", b"0e-" + b"9" * 1_048_573, True),
        ("raw_over", b" " * 1_048_577, False),
        ("late_failure", b'"' + b"a" * 1_048_574 + b"!", False),
        ("duplicate", b'{"a":1,"\\u0061":2}', False),
    ]
    for name, raw, accepted in raw_cases:
        identity = f"structured_{name}"
        actions.append(
            Case(identity, partial(parse_structured_output_json, raw), accepted)
        )
        manifest[identity] = {"raw_bytes": len(raw), "accepted": int(accepted)}
    return actions, manifest


def lifetime_cases() -> tuple[list[Case], list[Case], dict[str, dict[str, int]]]:
    value = parse_structured_output({"value": [1, -0.0, None]})
    successes = [
        Case("native", partial(parse_structured_output, {"value": [1, -0.0, None]})),
        Case("raw", partial(parse_structured_output_json, b'{"value":[1,-0.0,null]}')),
        Case("canonical", partial(canonical_structured_output, value)),
        Case("digest", partial(structured_output_digest, value)),
        Case("summary", partial(summarize_structured_output, value)),
        Case("null", partial(parse_structured_output, None)),
    ]
    refusals = [
        Case(
            "duplicate", partial(parse_structured_output_json, b'{"a":0,"a":1}'), False
        ),
        Case("unicode", partial(parse_structured_output_json, b'"\\ud800"'), False),
        Case("native", partial(parse_structured_output, (1,)), False),
        Case("nonfinite", partial(parse_structured_output, float("inf")), False),
        Case("unchecked", partial(canonical_structured_output, forged(b"1.00")), False),
        Case("raw_type", partial(parse_structured_output_json, b"["), False),
    ]
    common = [*successes, *successes, *refusals, *refusals]
    near_cap = ('"' + exact_string(1_048_576) + '"').encode()
    stress = [
        Case("near_cap", partial(parse_structured_output_json, near_cap)),
        Case(
            "large_native", partial(parse_structured_output, [None] * 1_048_576), False
        ),
        Case(
            "late_failure",
            partial(parse_structured_output_json, near_cap[:-1] + b"!"),
            False,
        ),
        Case("float", partial(parse_structured_output_json, near_cap_float())),
        Case(
            "overflow",
            partial(parse_structured_output_json, near_cap_float(True)),
            False,
        ),
    ]
    manifest = {
        "common": {"successes": 12, "refusals": 12},
        "stress": {"bytes": len(near_cap)},
    }
    return common, stress, manifest


def run(traced: bool, revision: str, identity: str) -> Lifecycle:
    common, stress, manifest = lifetime_cases()
    for _ in range(3):
        for case in (*common, *stress):
            exercise(case)
    storage = array("q", [0] * ((BATCHES + 1) * WIDTH))
    summaries = [bytearray(SUMMARY_BYTES), bytearray(SUMMARY_BYTES)]
    baseline: tracemalloc.Snapshot | None = None
    gc.collect()
    if traced:
        tracemalloc.start()
        baseline = tracemalloc.take_snapshot()
    sample(storage, 0, traced)

    started = time.perf_counter()
    for batch in range(BATCHES):
        for case in common:
            exercise(case)
        exercise(stress[batch % len(stress)])
        sample(storage, batch + 1, traced)
        if batch in (9, 19) and baseline is not None:
            retain_paths(baseline, summaries[batch // 10])
    elapsed = time.perf_counter() - started
    if traced:
        tracemalloc.stop()

    return Lifecycle(
        traced=traced,
        seconds=elapsed,
        successes=SUCCESSES,
        refusals=REFUSALS,
        points=[
            point(storage, index).model_copy(update={"cycle": index * INTERVAL})
            for index in range(BATCHES + 1)
        ],
        retaining_paths=[bytes(value).rstrip(b"\0").decode() for value in summaries],
        fixtures=manifest,
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
