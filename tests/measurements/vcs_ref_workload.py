import gc
import os
import sys
import time
import tracemalloc
from array import array
from collections.abc import Callable
from functools import partial
from hashlib import sha256
from typing import cast

import pydantic
import pydantic_core

from kora.ir import VcsRef
from kora.ir.vcs_ref import (
    VcsRefValidationError,
    canonical_vcs_ref,
    parse_vcs_ref,
    parse_vcs_ref_json,
    summarize_vcs_ref,
    vcs_ref_digest,
)
from kora.ir.vcs_ref_json import canonical_bytes, decode_vcs_ref_json
from tests.ir.vcs_ref_cases import (
    construct,
    document,
    exact_size,
    oversized_objects,
    principal,
    unchecked,
    wire,
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
POINTS: int = BATCHES + 1


def exercise(case: Case) -> None:
    try:
        case.action()
    except VcsRefValidationError:
        assert not case.accepted, case.name
    else:
        assert case.accepted, case.name


def full_boundary(payload: bytes) -> object:
    value = parse_vcs_ref_json(payload)
    encoded = canonical_vcs_ref(value)
    digest = vcs_ref_digest(value)
    summary = summarize_vcs_ref(value)
    assert sha256(encoded).hexdigest() == digest
    assert summary
    return value


def dimensions(value: dict[str, object]) -> dict[str, int]:
    paths = cast("list[object]", value["changed_paths"])
    return {
        "json_bytes": len(wire(value)),
        "root_fields": len(value),
        "paths": len(paths),
        "path_utf8_bytes": sum(
            len(path.encode()) for path in paths if type(path) is str
        ),
        "invalid_path_types": sum(type(path) is not str for path in paths),
        "diff_characters": len(cast("str", value["diff_stat"])),
        "commit_url_characters": len(cast("str", value.get("commit_url", ""))),
    }


def cases() -> tuple[list[Case], dict[str, dict[str, int]]]:
    actions: list[Case] = []
    manifest: dict[str, dict[str, int]] = {}
    for size in (1024, 65_536, 524_288, 1_048_575, 1_048_576):
        candidate = exact_size(size)
        payload = wire(candidate)
        value = parse_vcs_ref(candidate)
        projection = summarize_vcs_ref(value)
        factories: dict[str, Callable[[], object]] = {
            "decode": partial(decode_vcs_ref_json, payload),
            "object": partial(parse_vcs_ref, candidate),
            "raw": partial(parse_vcs_ref_json, payload),
            "canonical": partial(canonical_vcs_ref, value),
            "serialize": partial(canonical_bytes, projection),
            "digest": partial(vcs_ref_digest, value),
            "hash": lambda payload=payload: sha256(payload).hexdigest(),
            "summary": partial(summarize_vcs_ref, value),
            "full": partial(full_boundary, payload),
        }
        for phase, action in factories.items():
            name = f"vcs_{phase}_{size}"
            actions.append(Case(name, action))
            manifest[name] = dimensions(candidate)
    for name, candidate in oversized_objects().items():
        bypassed = unchecked(candidate)
        routes: dict[str, Callable[[], object]] = {
            "object": partial(parse_vcs_ref, candidate),
            "constructor": partial(construct, VcsRef, candidate),
            "model": partial(parse_vcs_ref, bypassed),
            "canonical": partial(canonical_vcs_ref, bypassed),
            "digest": partial(vcs_ref_digest, bypassed),
            "summary": partial(summarize_vcs_ref, bypassed),
        }
        lengths = dimensions(candidate)
        for route, action in routes.items():
            key = f"vcs_refused_{name}_{route}"
            actions.append(Case(key, action, False))
            manifest[key] = lengths
    paths = [f"prefix/{index:05d}" for index in range(10_000)]
    object_cases: dict[str, tuple[dict[str, object], bool]] = {
        "paths_10000": ({**document(), "changed_paths": paths}, True),
        "duplicate_early": (
            {**document(), "changed_paths": [paths[0], *paths[:-1]]},
            False,
        ),
        "duplicate_late": (
            {**document(), "changed_paths": [*paths[:-1], paths[0]]},
            False,
        ),
        "invalid_path_early": (
            {**document(), "changed_paths": ["../a", *paths[1:]]},
            False,
        ),
        "invalid_path_late": (
            {**document(), "changed_paths": [*paths[:-1], "../a"]},
            False,
        ),
        "multibyte": (exact_size(524_288, "😀"), True),
        "escaped": (exact_size(524_288, "\u0001"), True),
        "long_url": (
            {**document(), "commit_url": "https://example.test/" + "a" * 524_288},
            True,
        ),
        "long_host": (
            {**document(), "commit_url": "https://" + "a" * 524_288 + "/"},
            True,
        ),
        "long_host_invalid_late": (
            {**document(), "commit_url": "https://" + "a" * 524_288 + "%xx/"},
            False,
        ),
        "zero_port": (
            {
                **document(),
                "commit_url": "https://example.test:" + "0" * 10_000 + "443",
            },
            True,
        ),
    }
    for name, (candidate, accepted) in object_cases.items():
        actions.append(Case(f"vcs_{name}", partial(parse_vcs_ref, candidate), accepted))
        manifest[f"vcs_{name}"] = dimensions(candidate)
    dense_invalid: dict[str, list[object]] = {
        "paths": [f"../{index:05d}" for index in range(10_000)],
        "types": [{"invalid": index} for index in range(10_000)],
    }
    for name, entries in dense_invalid.items():
        candidate = {**document(), "changed_paths": entries}
        routes = {
            "object": partial(parse_vcs_ref, candidate),
            "constructor": partial(construct, VcsRef, candidate),
        }
        for route, action in routes.items():
            key = f"vcs_dense_invalid_{name}_{route}"
            actions.append(Case(key, action, False))
            manifest[key] = dimensions(candidate)
    payload = wire(document())
    dense = b"{" + b'"unknown":0,' * 80_000 + b'"last":0}'
    raw_cases = {
        "dense_unknown": b"{"
        + b",".join(f'"unknown{index}":0'.encode() for index in range(40_000))
        + b"}",
        "dense_duplicates": dense,
        "late_malformed": wire(exact_size(1_048_576))[:-1] + b"!",
        "depth_17": b"[" * 17 + b"]" * 17,
        "depth_16_shape": b"[" * 16 + b"]" * 16,
        "raw_overflow": payload + b" " * (1_048_577 - len(payload)),
        "quoted_brackets": b'{"unknown":"[{}]\\\\\\""}',
    }
    for name, raw in raw_cases.items():
        actions.append(Case(f"vcs_{name}", partial(parse_vcs_ref_json, raw), False))
        manifest[f"vcs_{name}"] = {"json_bytes": len(raw)}
    return actions, manifest


def lifetime_cases() -> tuple[list[Case], list[Case], dict[str, dict[str, int]]]:
    candidate = document()
    payload = wire(candidate)
    value = parse_vcs_ref(candidate)
    successes = [
        Case("object", partial(parse_vcs_ref, candidate)),
        Case("raw", partial(parse_vcs_ref_json, payload)),
        Case("canonical", partial(canonical_vcs_ref, value)),
        Case("digest", partial(vcs_ref_digest, value)),
        Case("summary", partial(summarize_vcs_ref, value)),
    ]
    refusals = [
        Case("duplicate", partial(parse_vcs_ref_json, b'{"a":0,"a":1}'), False),
        Case(
            "unknown", partial(parse_vcs_ref, {**candidate, "unknown": "value"}), False
        ),
        Case(
            "principal",
            partial(
                parse_vcs_ref,
                {**candidate, "pushed_by": {**principal(), "kind": "bad"}},
            ),
            False,
        ),
        Case(
            "semantic_url",
            partial(parse_vcs_ref, {**candidate, "commit_url": "https://[::1]suffix/"}),
            False,
        ),
        Case(
            "unchecked",
            partial(parse_vcs_ref, unchecked({**candidate, "commit": None})),
            False,
        ),
    ]
    encoded = [
        {**candidate, "diff_stat": "😀"},
        {**candidate, "diff_stat": "\b\f\n\r\t\u0001"},
        {**candidate, "commit_url": "https://example.test:" + "0" * 10_000},
    ]
    short_paths = {**candidate, "changed_paths": [f"{i:05d}" for i in range(10_000)]}
    common = [
        *successes,
        *successes,
        *refusals,
        *refusals,
        *(
            Case(f"encoding_{i}", partial(parse_vcs_ref, item))
            for i, item in enumerate(encoded)
        ),
        Case("paths_10000", partial(parse_vcs_ref, short_paths)),
    ]
    oversize = oversized_objects()
    dense = b"{" + b",".join(f'"extra{i}":0'.encode() for i in range(40_000)) + b"}"
    near_cap = exact_size(1_048_576)
    stress = [
        Case("near_cap", partial(parse_vcs_ref_json, wire(near_cap))),
        Case("dense_refusal", partial(parse_vcs_ref_json, dense), False),
        *(
            Case(name, partial(parse_vcs_ref, oversize[name]), False)
            for name in ("long_paths", "display", "url")
        ),
    ]
    manifest = {
        "small": dimensions(candidate),
        "paths_10000": dimensions(short_paths),
        "near_cap": dimensions(near_cap),
        "dense_refusal": {"json_bytes": len(dense)},
        **{
            name: dimensions(oversize[name])
            for name in ("long_paths", "display", "url")
        },
    }
    return common, stress, manifest


def run(traced: bool, revision: str, identity: str) -> Lifecycle:
    common, stress, manifest = lifetime_cases()
    for _ in range(3):
        for case in (*common, *stress):
            exercise(case)
    storage = array("q", [0] * (POINTS * WIDTH))
    summaries = [bytearray(SUMMARY_BYTES), bytearray(SUMMARY_BYTES)]
    baseline: tracemalloc.Snapshot | None = None
    gc.collect()
    if traced:
        tracemalloc.start()
        baseline = tracemalloc.take_snapshot()
    sample(storage, 0, traced)

    start = time.perf_counter()
    for batch in range(BATCHES):
        for case in common:
            exercise(case)
        exercise(stress[batch % len(stress)])
        sample(storage, batch + 1, traced)
        if batch in (9, 19) and baseline is not None:
            retain_paths(baseline, summaries[batch // 10])
    elapsed = time.perf_counter() - start
    if traced:
        tracemalloc.stop()
    points = [
        point(storage, index).model_copy(update={"cycle": index * INTERVAL})
        for index in range(POINTS)
    ]
    return Lifecycle(
        traced=traced,
        seconds=elapsed,
        successes=284,
        refusals=216,
        points=points,
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
