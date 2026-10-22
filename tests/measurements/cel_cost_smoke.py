import asyncio
import hashlib
import json
import os
import resource
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict, cast

from kora.cel import CheckedExpression, JsonValue, ValueBound, Variable, check
from kora.cel.types import DYN, INT, STRING, CelType, ListOf
from kora.cel_eval.errors import ErrorContext
from kora.cel_eval.thread import Evaluator

CASE_NAMES: tuple[str, ...] = (
    "ascii",
    "unicode",
    "escapes",
    "nested",
    "linear",
    "quadratic",
    "regex_success",
    "regex_late_failure",
    "regex_empty",
    "large_rendering",
)
TARGET_NS: int = 100_000_000
PRELUDE: str = (
    "import json,sys;sys.path[:]=json.loads(sys.argv[1]);"
    "from tests.measurements.cel_cost_smoke import run_smoke;run_smoke(sys.argv[2])"
)
AT: datetime = datetime(2026, 10, 23, tzinfo=UTC)


def clock() -> datetime:
    return AT


def digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class SmokeCase:
    name: str
    source: str
    values: dict[str, JsonValue]
    types: dict[str, CelType]
    environment: dict[str, Variable]
    expected: bool

    def checked(self) -> CheckedExpression:
        result = check(self.source, self.environment)
        assert isinstance(result, CheckedExpression), self.name
        return result

    def metadata(self) -> dict[str, object]:
        return {
            "case": self.name,
            "source_sha256": digest(self.source),
            "rendering_sha256": self.checked().digest,
            "input_sha256": digest(self.values),
            "variable_types": {key: repr(kind) for key, kind in self.types.items()},
            "expected": self.expected,
            "source_characters": len(self.source),
        }


def smoke_case(name: str) -> SmokeCase:
    if name not in CASE_NAMES:
        raise ValueError("undeclared CEL smoke case")
    if name == "large_rendering":
        return SmokeCase(name, "'" + "\U0001f600" * 4090 + "'==''", {}, {}, {}, False)
    if name == "nested":
        value: JsonValue = 0
        for _ in range(32):
            value = [value]
        return SmokeCase(
            name,
            "data == data",
            {"data": value},
            {"data": DYN},
            {"data": Variable(DYN, ValueBound(65, 33, 1, 0))},
            True,
        )
    if name in ("linear", "quadratic"):
        values: list[JsonValue] = [0] * 255
        kind = ListOf(INT)
        source = (
            "data.all(x, x == 0)"
            if name == "linear"
            else "data.map(x, x).filter(x, x == 0) == data"
        )
        return SmokeCase(
            name,
            source,
            {"data": values},
            {"data": kind},
            {
                "data": Variable(
                    kind, ValueBound(511, 256, 255, 0, ValueBound(1, 1, 0, 0))
                )
            },
            True,
        )
    text = {
        "ascii": "a" * 4094,
        "unicode": "\U0001f600" * 1023 + "aa",
        "escapes": "\x00" * 682 + "aa",
        "regex_success": "a" * 4094,
        "regex_late_failure": "a" * 4093 + "b",
        "regex_empty": "a" * 4094,
    }[name]
    source = "data == data"
    if name.startswith("regex_"):
        pattern = "" if name == "regex_empty" else "^(?:a|aa)*$"
        source = f"data.matches({json.dumps(pattern)})"
    return SmokeCase(
        name,
        source,
        {"data": text},
        {"data": STRING},
        {"data": Variable(STRING, ValueBound(4096, 1, 0, len(text.encode())))},
        name != "regex_late_failure",
    )


class SmokeSample(TypedDict):
    phase: str
    nanoseconds: int
    cache_entries_before: int
    cache_entries_after: int
    result: bool


async def observe(case: SmokeCase, payload: dict[str, object]) -> None:
    checked = case.checked()
    evaluator = Evaluator(clock)
    samples: list[SmokeSample] = []
    payload["samples"] = samples
    try:
        for iteration in range(4):
            before = evaluator.snapshot()
            started = time.perf_counter_ns()
            result = await evaluator.evaluate(
                checked,
                case.values,
                case.types,
                ErrorContext("measurements.cost_smoke"),
            )
            elapsed = time.perf_counter_ns() - started
            after = evaluator.snapshot()
            assert type(result) is bool
            samples.append(
                SmokeSample(
                    phase="cold" if iteration == 0 else "warm",
                    nanoseconds=elapsed,
                    cache_entries_before=before.cache_entries,
                    cache_entries_after=after.cache_entries,
                    result=result,
                )
            )
            assert result is case.expected
    finally:
        await evaluator.close()
        with evaluator.owner.condition:
            assert evaluator.owner.condition.wait_for(
                lambda: evaluator.snapshot().diagnostics == 0, 1
            )
        state = evaluator.snapshot()
        payload["resources"] = {
            "handles": state.handles,
            "outstanding": state.outstanding,
            "charged_bytes": state.bytes,
            "queued": state.queued,
            "running": state.running,
            "deliveries": state.deliveries,
            "diagnostics": state.diagnostics,
            "cache_entries": state.cache_entries,
            "cache_characters": state.cache_characters,
            "persistent_workers": sum(
                t.name == "kora-cel-evaluator" for t in threading.enumerate()
            ),
        }


def run_smoke(name: str) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    case = smoke_case(name)
    descriptors = len(os.listdir("/dev/fd"))
    previous = threading.stack_size()
    payload: dict[str, object] = {
        "schema": 1,
        **case.metadata(),
        "pid": os.getpid(),
        "descriptors_before": descriptors,
    }
    try:
        asyncio.run(observe(case, payload))
        payload["completion"] = "cel-cost-smoke-complete"
    finally:
        payload["descriptors_after"] = len(os.listdir("/dev/fd"))
        payload["stack_restored"] = previous == threading.stack_size()
        sys.stdout.write(json.dumps(payload))


def unique_fields(entries: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in entries:
        if key in result:
            raise ValueError("duplicate CEL smoke field")
        result[key] = value
    return result


def invalid_constant(value: str) -> object:
    raise ValueError("nonfinite CEL smoke value")


def validate_smoke(content: bytes, case: SmokeCase) -> dict[str, object]:
    raw: object = json.loads(
        content, object_pairs_hook=unique_fields, parse_constant=invalid_constant
    )
    if not isinstance(raw, dict):
        raise ValueError("invalid CEL smoke report")
    report = cast(dict[str, object], raw)
    expected = {
        "schema": 1,
        **case.metadata(),
        "stack_restored": True,
        "completion": "cel-cost-smoke-complete",
    }
    if set(report) != set(expected) | {
        "pid",
        "samples",
        "resources",
        "descriptors_before",
        "descriptors_after",
    } or any(
        json.dumps(report[key], sort_keys=True) != json.dumps(value, sort_keys=True)
        for key, value in expected.items()
    ):
        raise ValueError("CEL smoke identity does not match its case")
    for field in ("pid", "descriptors_before", "descriptors_after"):
        if type(report[field]) is not int or cast(int, report[field]) <= 0:
            raise ValueError("invalid CEL smoke resource counter")
    if report["descriptors_before"] != report["descriptors_after"]:
        raise ValueError("CEL smoke descriptors remain owned")
    resources = report["resources"]
    expected_resources = {
        key: 0
        for key in (
            "handles",
            "outstanding",
            "charged_bytes",
            "queued",
            "running",
            "deliveries",
            "diagnostics",
        )
    }
    expected_resources.update(
        cache_entries=1,
        cache_characters=len(case.checked().rendering),
        persistent_workers=1,
    )
    if json.dumps(resources, sort_keys=True) != json.dumps(
        expected_resources, sort_keys=True
    ):
        raise ValueError("CEL smoke resources were not released")
    raw_samples = report["samples"]
    if not isinstance(raw_samples, list):
        raise ValueError("incomplete CEL smoke samples")
    samples = cast(list[object], raw_samples)
    if len(samples) != 4:
        raise ValueError("incomplete CEL smoke samples")
    for iteration, raw_sample in enumerate(samples):
        if not isinstance(raw_sample, dict):
            raise ValueError("invalid CEL smoke sample")
        sample = cast(dict[str, object], raw_sample)
        expected_sample: dict[str, object] = {
            "phase": "cold" if iteration == 0 else "warm",
            "cache_entries_before": 0 if iteration == 0 else 1,
            "cache_entries_after": 1,
            "result": case.expected,
        }
        if (
            set(sample) != set(expected_sample) | {"nanoseconds"}
            or any(
                json.dumps(sample[key]) != json.dumps(value)
                for key, value in expected_sample.items()
            )
            or type(sample["nanoseconds"]) is not int
            or sample["nanoseconds"] <= 0
        ):
            raise ValueError("invalid CEL smoke sample completion")
    return report
