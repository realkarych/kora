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
    AgentSessionValidationError,
    agent_session_digest,
    canonical_agent_session,
    parse_agent_session,
    parse_agent_session_json,
    summarize_agent_session,
    verify_agent_session_transcript,
)
from tests.ir.agent_session_cases import (
    changed,
    dense,
    fixture,
    fixture_bytes,
    forged,
    with_transcript,
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
    except AgentSessionValidationError:
        assert not case.accepted, case.name
    else:
        assert case.accepted, case.name


def cases() -> tuple[list[Case], dict[str, dict[str, int]]]:
    actions: list[Case] = []
    manifest: dict[str, dict[str, int]] = {}
    for shape, source in (
        ("minimal", fixture("unavailable-no-usage")),
        ("dense", dense()),
    ):
        value = parse_agent_session(source)
        raw = canonical_agent_session(value)
        phases: dict[str, Callable[[], object]] = {
            "native": partial(parse_agent_session, source),
            "raw": partial(parse_agent_session_json, raw),
            "canonical": partial(canonical_agent_session, value),
            "digest": partial(agent_session_digest, value),
            "summary": partial(summarize_agent_session, value),
        }
        for phase, action in phases.items():
            name = f"session_{shape}_{phase}"
            actions.append(Case(name, action))
            manifest[name] = {"canonical_bytes": len(raw)}
    wire = canonical_agent_session(parse_agent_session(fixture()))
    raw_cases = (
        ("raw_at", wire + b" " * (4194304 - len(wire)), True),
        ("raw_over", b" " * 4194305, False),
        ("late_failure", wire + b" " * (4194303 - len(wire)) + b"!", False),
        ("depth_over", b"[" * 17 + b"]" * 17, False),
        ("duplicate", wire[:-1] + b',"metadata":{}}', False),
    )
    for label, raw, accepted in raw_cases:
        name = f"session_{label}"
        actions.append(Case(name, partial(parse_agent_session_json, raw), accepted))
        manifest[name] = {"raw_bytes": len(raw), "accepted": int(accepted)}
    for label, candidate in (
        ("root_count", {str(index): None for index in range(10000)}),
        ("usage_over", changed(("metadata", "usage"), [None] * 10000)),
        ("denials_over", changed(("denials", "items"), [None] * 10000)),
        ("identifier_over", changed(("metadata", "harness"), "a" * 4194305)),
        ("count_over", changed(("metadata", "duration_ms"), 2**63)),
        ("float", changed(("metadata", "duration_ms"), 1.0)),
    ):
        name = f"session_{label}"
        actions.append(Case(name, partial(parse_agent_session, candidate), False))
        manifest[name] = {"accepted": 0}
    actions.append(
        Case(
            "session_forged",
            partial(canonical_agent_session, forged(wire + b"\n")),
            False,
        )
    )
    manifest["session_forged"] = {"accepted": 0}
    for size in (0, 1, 24, 4097, 268435456):
        payload = b"a" * size
        value = parse_agent_session(with_transcript(payload))
        name = f"session_capture_{size}"
        actions.append(
            Case(name, partial(verify_agent_session_transcript, value, payload))
        )
        manifest[name] = {"capture_bytes": size, "accepted": 1}
    value = parse_agent_session(fixture())
    actions.append(
        Case(
            "session_capture_over",
            partial(verify_agent_session_transcript, value, b"a" * 268435457),
            False,
        )
    )
    manifest["session_capture_over"] = {"capture_bytes": 268435457, "accepted": 0}
    return actions, manifest


def lifetime_cases() -> tuple[list[Case], list[Case], dict[str, dict[str, int]]]:
    source = fixture()
    raw = fixture_bytes()
    value = parse_agent_session(source)
    successes = [
        Case("native", partial(parse_agent_session, source)),
        Case("raw", partial(parse_agent_session_json, raw)),
        Case("canonical", partial(canonical_agent_session, value)),
        Case("digest", partial(agent_session_digest, value)),
        Case("summary", partial(summarize_agent_session, value)),
        Case("capture", partial(verify_agent_session_transcript, value, b"")),
    ]
    refusals = [
        Case(
            "duplicate",
            partial(parse_agent_session_json, raw[:-1] + b',"metadata":{}}'),
            False,
        ),
        Case("unicode", partial(parse_agent_session_json, b'"\\ud800"'), False),
        Case(
            "native",
            partial(parse_agent_session, changed(("metadata", "duration_ms"), True)),
            False,
        ),
        Case("shape", partial(parse_agent_session, {}), False),
        Case("forged", partial(agent_session_digest, forged(raw + b"\n")), False),
        Case("capture", partial(verify_agent_session_transcript, value, b"a"), False),
    ]
    common = [*successes, *successes, *refusals, *refusals]
    large = dense()
    capture = b"a" * 268435456
    captured = parse_agent_session(with_transcript(capture))
    stress = [
        Case("dense", partial(parse_agent_session, large)),
        Case(
            "capture_cap", partial(verify_agent_session_transcript, captured, capture)
        ),
        Case(
            "native_over",
            partial(
                parse_agent_session, changed(("denials", "items"), [None] * 1048576)
            ),
            False,
        ),
        Case(
            "late_failure",
            partial(parse_agent_session_json, raw + b" " * (4194303 - len(raw)) + b"!"),
            False,
        ),
        Case("depth", partial(parse_agent_session_json, b"[" * 17 + b"]" * 17), False),
    ]
    manifest = {
        "common": {"successes": 12, "refusals": 12},
        "stress": {
            "dense_canonical_bytes": len(
                canonical_agent_session(parse_agent_session(large))
            ),
            "capture_bytes": len(capture),
        },
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
