import asyncio
import hashlib
import json
import resource
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict

from kora.cel import CheckedExpression, JsonValue, ValueBound, Variable, check
from kora.cel.types import DOUBLE, DYN, CelType, ListOf
from kora.cel_eval.binding import load_binding
from kora.cel_eval.bounds import Size, measure
from kora.cel_eval.convert import BoundValues, bind, convert
from kora.cel_eval.errors import ErrorContext, ErrorKind
from kora.cel_eval.thread import EvaluationThread, Evaluator

WARMUP: int = 3
SAMPLES: int = 15
SCALES: tuple[int, ...] = (1, 2, 4, 8)
FULL_TIMINGS: tuple[str, ...] = (
    "precheck_ns",
    "copy_ns",
    "postcheck_ns",
    "bind_ns",
    "production_ns",
    "hop_ns",
)
NATIVE_TIMINGS: tuple[str, ...] = ("native_ns", "control_ns")
AT: datetime = datetime(2026, 10, 22, tzinfo=UTC)


def clock() -> datetime:
    return AT


@dataclass(frozen=True)
class PilotCase:
    family: str
    scale: int
    value: JsonValue
    type: CelType

    @property
    def name(self) -> str:
        return f"{self.family}-{self.scale}"

    def checked(self) -> CheckedExpression:
        return self.workload().checked()

    def workload(self) -> "ConversionCase":
        assert isinstance(self.value, list)
        source = f"values.size() == {len(self.value)}"
        if self.family == "numbers":
            source += " && values[0][0] == 0.0"
        return ConversionCase(
            self.name,
            source,
            {"values": self.value},
            {"values": self.type},
            {"values": []},
            1,
            65_536,
            32_769,
            ("historical_recipe",),
        )

    def original_size(self) -> Size:
        size = measure(self.value)
        assert isinstance(size, Size)
        return size

    def digest(self) -> str:
        data = json.dumps(self.value, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(data.encode("utf-8")).hexdigest()


def chain() -> JsonValue:
    value: JsonValue = []
    for _ in range(31):
        value = [value]
    return value


def pilot_cases() -> tuple[PilotCase, ...]:
    result: list[PilotCase] = []
    for family in (
        "ascii",
        "unicode",
        "escapes",
        "empty_lists",
        "small_maps",
        "deep_chains",
        "numbers",
    ):
        for scale in SCALES:
            count = 125 * scale
            kind: CelType = DYN
            value: JsonValue
            if family == "ascii":
                value = ["a" * (2047 * scale) for _ in range(4)]
            elif family == "unicode":
                value = ["\U0001f642" * (511 * scale) for _ in range(4)]
            elif family == "escapes":
                value = ["\x00" * (341 * scale) for _ in range(4)]
            elif family == "empty_lists":
                value = [[[] for _ in range(21)] for _ in range(count)]
            elif family == "small_maps":
                value = [
                    {f"k{index}": None for index in range(6)} for _ in range(count)
                ]
            elif family == "deep_chains":
                value = [chain() for _ in range(count)]
            else:
                value = [list(range(14)) for _ in range(count)]
                kind = ListOf(ListOf(DOUBLE))
            result.append(PilotCase(family, scale, value, kind))
    return tuple(result)


def value_depth(value: JsonValue) -> int:
    pending: list[tuple[JsonValue, int]] = [(value, 0)]
    deepest = 0
    while pending:
        current, depth = pending.pop()
        deepest = max(deepest, depth)
        if isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)
        elif isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
    return deepest


def digest(value: object) -> str:
    serialized = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def binding_statistics(values: dict[str, JsonValue]) -> tuple[int, int, int]:
    canonical_bytes = sum(
        len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
        for value in values.values()
    )
    pending: list[tuple[JsonValue, int]] = [(value, 0) for value in values.values()]
    count = 0
    deepest = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        deepest = max(deepest, depth)
        if isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
        elif isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
    return canonical_bytes, count, deepest


class ConversionMetadata(TypedDict):
    original_bytes: int
    converted_bytes: int
    original_values: int
    values: int
    original_depth: int
    converted_depth: int
    input_sha256: str
    converted_sha256: str
    control_sha256: str
    expression: str
    rendering_sha256: str
    variable_types: dict[str, str]
    byte_limit: int
    value_limit: int
    limiting_constraints: list[str]


@dataclass(frozen=True)
class ConversionCase:
    name: str
    source: str
    bindings: dict[str, JsonValue]
    types: dict[str, CelType]
    control: dict[str, JsonValue]
    batch: int
    byte_limit: int
    value_limit: int
    constraints: tuple[str, ...]

    def checked(self) -> CheckedExpression:
        environment: dict[str, Variable] = {}
        for name, value in self.bindings.items():
            size, count, _ = binding_statistics({name: value})
            bound = ValueBound(3 * size, count, 1000, 16384)
            environment[name] = Variable(self.types[name], bound)
        result = check(self.source, environment)
        assert isinstance(result, CheckedExpression)
        return result

    def converted(self) -> dict[str, JsonValue]:
        values: dict[str, JsonValue] = {}
        for name, value in self.bindings.items():
            result = convert(value, self.types[name])
            assert not isinstance(result, ErrorKind)
            values[name] = result
        return values

    def metadata(self) -> ConversionMetadata:
        converted = self.converted()
        before = binding_statistics(self.bindings)
        after = binding_statistics(converted)
        if (
            max(before[0], after[0]) > self.byte_limit
            or max(before[1], after[1]) > self.value_limit
            or max(before[2], after[2]) > 32
        ):
            raise ValueError("conversion catalogue exceeds its declared bounds")
        return ConversionMetadata(
            original_bytes=before[0],
            converted_bytes=after[0],
            original_values=before[1],
            values=after[1],
            original_depth=before[2],
            converted_depth=after[2],
            input_sha256=digest(self.bindings),
            converted_sha256=digest(converted),
            control_sha256=digest(self.control),
            expression=self.source,
            rendering_sha256=hashlib.sha256(
                self.checked().rendering.encode()
            ).hexdigest(),
            variable_types={name: repr(kind) for name, kind in self.types.items()},
            byte_limit=self.byte_limit,
            value_limit=self.value_limit,
            limiting_constraints=list(self.constraints),
        )


def measured_size(values: dict[str, JsonValue]) -> Size:
    sizes = [measure(value) for value in values.values()]
    assert all(isinstance(size, Size) for size in sizes)
    return Size(
        sum(size.bytes for size in sizes if isinstance(size, Size)),
        sum(size.values for size in sizes if isinstance(size, Size)),
    )


def prepared(case: ConversionCase) -> BoundValues:
    value = bind(case.checked(), case.bindings, case.types)
    assert isinstance(value, BoundValues)
    return value


def append_time(timings: dict[str, list[int]], name: str, started: int) -> None:
    timings[name].append(time.perf_counter_ns() - started)


async def full_samples(
    case: ConversionCase,
) -> tuple[dict[str, list[int]], dict[str, int]]:
    checked = case.checked()
    reference = check("true")
    assert isinstance(reference, CheckedExpression)
    values = case.bindings
    types = case.types
    context = ErrorContext("measurements.conversion")
    evaluator = Evaluator(clock)
    timings: dict[str, list[int]] = {name: [] for name in FULL_TIMINGS}
    try:
        for _ in range(WARMUP + SAMPLES):
            started = time.perf_counter_ns()
            before = measured_size(values)
            append_time(timings, "precheck_ns", started)
            assert isinstance(before, Size)

            started = time.perf_counter_ns()
            copied = case.converted()
            append_time(timings, "copy_ns", started)

            started = time.perf_counter_ns()
            after = measured_size(copied)
            append_time(timings, "postcheck_ns", started)
            assert isinstance(after, Size)

            started = time.perf_counter_ns()
            bound = bind(checked, values, types)
            append_time(timings, "bind_ns", started)
            assert isinstance(bound, BoundValues)
            assert bound.bytes == after.bytes
            assert bound.count == after.values

            started = time.perf_counter_ns()
            outcome = await evaluator.evaluate(checked, values, types, context)
            append_time(timings, "production_ns", started)
            assert outcome is True

            started = time.perf_counter_ns()
            baseline = await evaluator.evaluate(reference, {}, {}, context)
            append_time(timings, "hop_ns", started)
            assert baseline is True
    finally:
        await evaluator.close()
    with evaluator.owner.condition:
        assert evaluator.owner.condition.wait_for(
            lambda: evaluator.snapshot().diagnostics == 0, 1.0
        )
    state = evaluator.snapshot()
    assert state.handles == state.outstanding == state.bytes == 0
    return timings, {
        "handles": state.handles,
        "outstanding": state.outstanding,
        "charged_bytes": state.bytes,
        "persistent_workers": sum(
            thread.name == "kora-cel-evaluator" for thread in threading.enumerate()
        ),
    }


class NativeConversionProbe(EvaluationThread):
    def __init__(self, case: ConversionCase) -> None:
        self.case: ConversionCase = case
        self.timings: dict[str, list[int]] = {name: [] for name in NATIVE_TIMINGS}
        self.completed: bool = False
        super().__init__(load_binding())

    def _run(self) -> None:
        checked = self.case.checked()
        values = prepared(self.case)
        native = self.machine.binding.compile(checked)
        for _ in range(WARMUP + SAMPLES):
            started = time.perf_counter_ns()
            for _ in range(self.case.batch):
                result = self.machine.binding.execute(native, values.values)
                assert result is True
            append_time(self.timings, "native_ns", started)

            started = time.perf_counter_ns()
            for _ in range(self.case.batch):
                control = self.machine.binding.execute(native, self.case.control)
                assert control is False
            append_time(self.timings, "control_ns", started)
        self.completed = True


def run_case(name: str, mode: str, catalogue: str = "pilot") -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if catalogue == "pilot":
        case = next(item.workload() for item in pilot_cases() if item.name == name)
    elif catalogue == "bounds":
        from tests.measurements.cel_bound_workload import case_by_name

        case = case_by_name(name)
    else:
        raise ValueError("unknown conversion catalogue")
    metadata = case.metadata()
    previous_stack = threading.stack_size()
    if mode == "full":
        timings, resources = asyncio.run(full_samples(case))
    elif mode == "native":
        probe = NativeConversionProbe(case)
        probe.worker.join(9.0)
        assert not probe.worker.is_alive()
        assert probe.completed
        timings = probe.timings
        resources = {"persistent_workers": 0}
    else:
        raise ValueError("unknown conversion probe mode")
    assert threading.stack_size() == previous_stack
    payload: dict[str, object] = {
        "schema": 2,
        "case": case.name,
        "mode": mode,
        **metadata,
        "warmup": WARMUP,
        "samples": SAMPLES,
        "calls": {name: 1 if mode == "full" else case.batch for name in timings},
        "timings": timings,
        "resources": resources,
        "stack_restored": True,
        "completion": "cel-conversion-complete",
    }
    sys.stdout.write(json.dumps(payload))
