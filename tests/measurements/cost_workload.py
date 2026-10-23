import gc
import json
import os
import sys
import threading
import time
import tracemalloc
from array import array
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Literal, cast

import pydantic
import pydantic_core
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticCustomError

from kora.ir.cost import (
    MAX_TOKEN,
    MoneyOverflow,
    PriceRevision,
    RunCost,
    UsageCoverage,
    UsageRow,
    accumulate_attempt_models,
    parse_money,
)
from kora.ir.pricing import estimate_usage_usd
from tests.ir.test_cost_models import (
    model_payload,
    rates_payload,
    revision_payload,
    revision_with_encoded_size,
    run_payload,
    usage_payload,
)
from tests.measurements.harness import Report, Sample, new_report, open_descriptors
from tests.measurements.ir_config_lifetime import (
    model_references,
    peak_rss_bytes,
    rss_bytes,
)

NOW: str = "2026-10-23T00:00:00Z"
WARM_UP: int = 3
MEASURED: int = 30
DENSE_MEASURED: int = 10
ORDINARY_CYCLES: int = 1000
LARGE_CYCLES: int = 3
POINTS: int = 4
WIDTH: int = 7
SUMMARY_BYTES: int = 2048
Group = Literal["ordinary", "large"]


@dataclass(frozen=True)
class CostCase:
    name: str
    action: Callable[[], object]
    accepted: bool = True
    group: Group = "ordinary"
    records: int = 1


def exercise_cost(case: CostCase) -> int:
    try:
        result = case.action()
    except (ValidationError, PydanticCustomError, MoneyOverflow):
        assert not case.accepted, case.name
        return 0
    assert case.accepted, case.name
    references = model_references(result)
    if isinstance(result, BaseModel):
        for name in type(result).model_fields:
            value = getattr(result, name)
            if isinstance(value, Mapping):
                references.extend(
                    reference
                    for item in cast("Mapping[object, object]", value).values()
                    for reference in model_references(item)
                )
            del value
    del result
    return sum(reference() is not None for reference in references)


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def snapshot_document(count: int) -> dict[str, object]:
    return run_payload(
        as_of=NOW,
        observed_at=NOW,
        models=[model_payload(model=f"model-{index:04d}") for index in range(count)],
    )


def snapshot_at_bytes(size: int) -> dict[str, object]:
    document = snapshot_document(1024)
    models = cast("list[dict[str, object]]", document["models"])
    remaining = size - len(canonical(document).encode())
    for model in models:
        name = cast("str", model["model"])
        width = min(remaining // 4, 256 - len(name))
        name += "😀" * width
        remaining -= width * 4
        if remaining < 4 and remaining <= 256 - len(name):
            name += "a" * remaining
            remaining = 0
        model["model"] = name
    assert remaining == 0
    assert len(canonical(document).encode()) == size
    return document


def union_models(count: int, refuse: bool = False) -> object:
    accumulated: frozenset[str] = frozenset()
    for index in range(count):
        accumulated = accumulate_attempt_models(accumulated, f"model-{index}")
    assert len(accumulated) == count
    assert accumulate_attempt_models(accumulated, "model-0") == accumulated
    if refuse:
        return accumulate_attempt_models(accumulated, "extra-model")
    return accumulated


def priced(row: UsageRow, revision: PriceRevision, expected: str | None) -> object:
    result = estimate_usage_usd(row, revision)
    assert result == expected
    return result


def round_trip(model: type[BaseModel], document: object) -> object:
    value = model.model_validate(document)
    return model.model_validate_json(value.model_dump_json())


def cost_cases() -> list[CostCase]:
    actions = [
        CostCase(f"union_{count}", partial(union_models, count))
        for count in (1, 16, 64)
    ]
    actions.append(CostCase("union_65_refused", partial(union_models, 64, True), False))
    coverage: dict[str, object] = {
        "version": 1,
        "observation_id": "observation",
        "observation_sequence": 0,
        "completeness": "complete",
        "observed_at": NOW,
        "attribution_gap": False,
        "observation_gap": False,
    }
    for name, document, valid in (
        ("complete", coverage, True),
        (
            "initial",
            {
                **coverage,
                "observation_id": None,
                "observation_sequence": None,
                "observed_at": None,
                "completeness": "unavailable",
            },
            True,
        ),
        ("gap_refused", {**coverage, "observation_gap": True}, False),
        ("sequence_refused", {**coverage, "observation_sequence": True}, False),
    ):
        actions.append(
            CostCase(
                f"coverage_{name}",
                partial(UsageCoverage.model_validate, document),
                valid,
            )
        )
    for name, document, valid in (
        ("valid", usage_payload(observed_at=NOW), True),
        ("tokens_refused", usage_payload(observed_at=NOW, input_tokens=True), False),
        (
            "ttl_refused",
            usage_payload(
                observed_at=NOW,
                cache_creation_tokens=3,
                cache_creation_by_ttl={"5m": 1, "1h": 1},
            ),
            False,
        ),
        (
            "reasoning_refused",
            usage_payload(observed_at=NOW, reasoning_tokens=1),
            False,
        ),
    ):
        actions.append(
            CostCase(
                f"observation_{name}", partial(UsageRow.model_validate, document), valid
            )
        )
    for count in (1, 64, 1024):
        document = snapshot_document(count)
        group: Group = "ordinary" if count == 1 else "large"
        for operation, action in (
            ("validate", partial(RunCost.model_validate, document)),
            ("round_trip", partial(round_trip, RunCost, document)),
        ):
            actions.append(
                CostCase(f"snapshot_{count}_{operation}", action, True, group, count)
            )
    for size in (65535, 65536, 65537):
        document = revision_with_encoded_size(NOW, size)
        for operation, action in (
            ("validate", partial(PriceRevision.model_validate, document)),
            ("parse", partial(PriceRevision.model_validate_json, canonical(document))),
        ):
            actions.append(
                CostCase(
                    f"revision_{size}_{operation}", action, size <= 65536, "large", 200
                )
            )
    for size in (8191, 8192, 8193):
        source = {"x": "x" * (size - 8)}
        document = usage_payload(observed_at=NOW, source=source)
        actions.append(
            CostCase(
                f"source_{size}",
                partial(UsageRow.model_validate, document),
                size <= 8192,
                "large",
            )
        )
    for size in (1048575, 1048576, 1048577):
        document = snapshot_at_bytes(size)
        for operation, action in (
            ("validate", partial(RunCost.model_validate, document)),
            ("parse", partial(RunCost.model_validate_json, canonical(document))),
        ):
            actions.append(
                CostCase(
                    f"snapshot_bytes_{size}_{operation}",
                    action,
                    size <= 1048576,
                    "large",
                    1024,
                )
            )
    actions.append(
        CostCase(
            "snapshot_1025_refused",
            partial(RunCost.model_validate, snapshot_document(1025)),
            False,
            "large",
            1025,
        )
    )
    for name, action in (
        ("money", partial(parse_money, "9" * 1_000_000)),
        (
            "identifier",
            partial(
                UsageRow.model_validate,
                usage_payload(observed_at=NOW, model="m" * 1_000_000),
            ),
        ),
    ):
        actions.append(CostCase(f"lexical_{name}_refused", action, False, "large"))
    revision = PriceRevision.model_validate(
        revision_payload({"model": rates_payload("10000000000.0000000001")})
    )
    row = UsageRow.model_validate(
        usage_payload(
            observed_at=NOW,
            input_tokens=MAX_TOKEN,
            cached_input_tokens=MAX_TOKEN,
            cache_creation_tokens=MAX_TOKEN,
            output_tokens=MAX_TOKEN,
            reasoning_tokens=MAX_TOKEN // 2,
            cache_creation_by_ttl={
                "5m": MAX_TOKEN // 2,
                "1h": MAX_TOKEN - MAX_TOKEN // 2,
            },
        )
    )
    actions.append(
        CostCase(
            "pricing_max_products",
            partial(priced, row, revision, "85899345880000.000000859"),
        )
    )
    overflow = PriceRevision.model_validate(
        revision_payload({"model": rates_payload("99999999999999.9999999999")})
    )
    actions.append(
        CostCase(
            "pricing_overflow_refused",
            partial(estimate_usage_usd, row, overflow),
            False,
        )
    )
    unknown = UsageRow.model_validate(
        usage_payload(observed_at=NOW, known_token_fields=[])
    )
    actions.append(
        CostCase("pricing_unknown", partial(priced, unknown, revision, None))
    )
    subscription = UsageRow.model_validate(
        usage_payload(
            observed_at=NOW,
            billing_class="subscription",
            reported_usd="10",
            basis="harness_list",
        )
    )
    actions.append(
        CostCase("pricing_subscription", partial(priced, subscription, revision, None))
    )
    return actions


def environment(report: Report) -> None:
    report.environment.update(
        pydantic=pydantic.__version__,
        pydantic_core=pydantic_core.__version__,
        executable=sys.executable,
        cwd=os.getcwd(),
        warm_up=WARM_UP,
        ordinary_measured=MEASURED,
        large_measured=DENSE_MEASURED,
        operations_per_batch=1,
        ordinary_cycles=ORDINARY_CYCLES,
        large_cycles=LARGE_CYCLES,
        rss_source="/bin/ps rss KiB converted to bytes"
        if sys.platform == "darwin"
        else "/proc/self/statm resident pages converted to bytes",
        peak_rss_source="getrusage RUSAGE_SELF ru_maxrss converted to bytes",
        unowned_resources="no tasks, connections, timers or subscriptions acquired",
    )


def timing(actions: list[CostCase], report: Report, representative: bool) -> None:
    for case in actions:
        for _ in range(WARM_UP):
            assert exercise_cost(case) == 0
        measured = (
            1
            if representative
            else MEASURED
            if case.group == "ordinary"
            else DENSE_MEASURED
        )
        for repeat in range(measured):
            started = time.perf_counter()
            live_models = exercise_cost(case)
            elapsed = time.perf_counter() - started
            assert live_models == 0
            report.samples.append(
                Sample(f"cost_{case.name}", repeat, case.records, elapsed)
            )


def lifetime(
    actions: list[CostCase],
    report: Report,
    traced: bool,
    group: Group,
    selected_name: str | None = None,
) -> None:
    selected = [
        case
        for case in actions
        if case.group == group and (selected_name is None or case.name == selected_name)
    ]
    assert selected
    cycles = ORDINARY_CYCLES if group == "ordinary" else LARGE_CYCLES
    checkpoints = [0, cycles // 3, cycles * 2 // 3, cycles]
    for _ in range(WARM_UP):
        for case in selected:
            assert exercise_cost(case) == 0
    storage = array("q", [0] * (POINTS * WIDTH))
    summaries = [bytearray(SUMMARY_BYTES) for _ in range(POINTS - 1)]
    baseline: tracemalloc.Snapshot | None = None
    if traced:
        tracemalloc.start()
        baseline = tracemalloc.take_snapshot()
    started = time.perf_counter()
    for cycle in range(cycles + 1):
        if cycle:
            for case in selected:
                assert exercise_cost(case) == 0
        if cycle not in checkpoints:
            continue
        index = checkpoints.index(cycle)
        gc.collect()
        heap, peak = tracemalloc.get_traced_memory() if traced else (-1, -1)
        values = (
            heap,
            peak,
            tracemalloc.get_tracemalloc_memory() if traced else -1,
            -1 if traced else rss_bytes(),
            -1 if traced else peak_rss_bytes(),
            open_descriptors(),
            threading.active_count(),
        )
        for column, value in enumerate(values):
            storage[index * WIDTH + column] = value
        if index and baseline is not None:
            snapshot = tracemalloc.take_snapshot()
            differences = snapshot.compare_to(baseline, "traceback")
            paths = "\n".join(
                f"{item.size_diff} bytes {item.count_diff} blocks: {item.traceback}"
                for item in differences[:8]
            )
            encoded = paths.encode()[:SUMMARY_BYTES]
            memoryview(summaries[index - 1])[: len(encoded)] = encoded
            del snapshot, differences, paths, encoded
    elapsed = time.perf_counter() - started
    if traced:
        tracemalloc.stop()
    report.environment["traced"] = traced
    report.environment["cycles"] = cycles
    report.environment["successes"] = cycles * sum(case.accepted for case in selected)
    report.environment["refusals"] = cycles * sum(
        not case.accepted for case in selected
    )
    report.environment["retaining_paths"] = canonical(
        [bytes(value).rstrip(b"\0").decode() for value in summaries]
    )
    report.environment["checkpoints"] = canonical(
        [
            {
                "cycle": cycle,
                **{
                    name: int(storage[index * WIDTH + column])
                    for column, name in enumerate(
                        (
                            "heap",
                            "peak",
                            "tracer",
                            "rss",
                            "high_water",
                            "descriptors",
                            "threads",
                        )
                    )
                },
            }
            for index, cycle in enumerate(checkpoints)
        ]
    )
    report.samples.append(
        Sample(
            f"cost_{group}_{'traced' if traced else 'native'}_lifetime",
            0,
            cycles,
            elapsed,
        )
    )


def main() -> None:
    if (
        len(sys.argv) not in {5, 6}
        or sys.argv[1]
        not in {"representative", "representative_traced", "timing", "traced", "native"}
        or sys.argv[2] not in {"ordinary", "large"}
    ):
        raise ValueError("expected mode, group, revision and run identity")
    mode, group, revision, identity = sys.argv[1:5]
    report = new_report()
    report.revision = revision
    report.environment["run"] = identity
    report.environment["pid"] = os.getpid()
    environment(report)
    actions = cost_cases()
    if mode in {"representative", "representative_traced", "timing"}:
        if mode == "representative_traced":
            tracemalloc.start()
        timing(
            [case for case in actions if case.group == group],
            report,
            mode != "timing",
        )
        if mode == "representative_traced":
            tracemalloc.stop()
    else:
        lifetime(
            actions,
            report,
            mode == "traced",
            cast("Group", group),
            sys.argv[5] if len(sys.argv) == 6 else None,
        )
    sys.stdout.write(report.to_json())


if __name__ == "__main__":
    main()
