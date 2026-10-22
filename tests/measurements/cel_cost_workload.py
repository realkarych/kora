import asyncio
import hashlib
import json
import resource
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from kora.cel import CheckedExpression, JsonValue, Variable, check
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
        assert isinstance(self.value, list)
        source = f"values.size() == {len(self.value)}"
        if self.family == "numbers":
            source += " && values[0][0] == 0.0"
        result = check(source, {"values": Variable(self.type)})
        assert isinstance(result, CheckedExpression)
        return result

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


def prepared(case: PilotCase) -> BoundValues:
    value = bind(case.checked(), {"values": case.value}, {"values": case.type})
    assert isinstance(value, BoundValues)
    return value


def append_time(timings: dict[str, list[int]], name: str, started: int) -> None:
    timings[name].append(time.perf_counter_ns() - started)


async def full_samples(case: PilotCase) -> tuple[dict[str, list[int]], dict[str, int]]:
    checked = case.checked()
    reference = check("true")
    assert isinstance(reference, CheckedExpression)
    values: dict[str, object] = {"values": case.value}
    types = {"values": case.type}
    context = ErrorContext("measurements.conversion")
    evaluator = Evaluator(clock)
    timings: dict[str, list[int]] = {name: [] for name in FULL_TIMINGS}
    try:
        for _ in range(WARMUP + SAMPLES):
            started = time.perf_counter_ns()
            before = measure(case.value)
            append_time(timings, "precheck_ns", started)
            assert isinstance(before, Size)

            started = time.perf_counter_ns()
            copied = convert(case.value, case.type)
            append_time(timings, "copy_ns", started)
            assert not isinstance(copied, ErrorKind)

            started = time.perf_counter_ns()
            after = measure(copied)
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
    def __init__(self, case: PilotCase) -> None:
        self.case: PilotCase = case
        self.timings: dict[str, list[int]] = {name: [] for name in NATIVE_TIMINGS}
        self.completed: bool = False
        super().__init__(load_binding())

    def _run(self) -> None:
        checked = self.case.checked()
        values = prepared(self.case)
        native = self.machine.binding.compile(checked)
        for _ in range(WARMUP + SAMPLES):
            started = time.perf_counter_ns()
            result = self.machine.binding.execute(native, values.values)
            append_time(self.timings, "native_ns", started)
            assert result is True

            started = time.perf_counter_ns()
            control = self.machine.binding.execute(native, {"values": []})
            append_time(self.timings, "control_ns", started)
            assert control is False
        self.completed = True


def run_case(name: str, mode: str) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    case = next(item for item in pilot_cases() if item.name == name)
    before = case.original_size()
    converted = prepared(case)
    converted_bytes, converted_values = converted.bytes, converted.count
    del converted
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
        "schema": 1,
        "case": case.name,
        "mode": mode,
        "input_sha256": case.digest(),
        "original_bytes": before.bytes,
        "converted_bytes": converted_bytes,
        "values": converted_values,
        "warmup": WARMUP,
        "samples": SAMPLES,
        "timings": timings,
        "resources": resources,
        "stack_restored": True,
        "completion": "cel-conversion-complete",
    }
    sys.stdout.write(json.dumps(payload))
