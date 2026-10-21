import datetime
import math
from typing import cast

from kora.cel_eval.binding import Binding
from tests.cel.binding_support import Typed, outcome
from tests.cel.conformance.cases import (
    BoolValue,
    BytesValue,
    Case,
    DoubleValue,
    DurationValue,
    ExpectedError,
    IntValue,
    ListValue,
    MapValue,
    NullValue,
    ObjectValue,
    StringValue,
    TypeValue,
    UintValue,
    Value,
)

TIMESTAMP_TYPE_URL: str = "type.googleapis.com/google.protobuf.Timestamp"
NANOS_PER_MICROSECOND: int = 1000


class Unbindable(Exception):
    pass


def bound(value: Value) -> object:
    match value:
        case NullValue():
            return None
        case BoolValue(value=flag):
            return flag
        case IntValue(value=number) | UintValue(value=number):
            return number
        case DoubleValue(value=number):
            return number
        case StringValue(value=text):
            return text
        case BytesValue(value=data):
            return data
        case TypeValue(name=name):
            return name
        case ListValue(values=values):
            return [bound(item) for item in values]
        case MapValue(entries=entries):
            return {bound(key): bound(item) for key, item in entries}
        case DurationValue(seconds=seconds, nanos=nanos):
            return datetime.timedelta(
                seconds=seconds, microseconds=nanos // NANOS_PER_MICROSECOND
            )
        case ObjectValue():
            raise Unbindable(value.type_url)


def same_double(expected: float, actual: object) -> bool:
    if not isinstance(actual, float):
        return False
    return expected == actual or (math.isnan(expected) and math.isnan(actual))


def same_entries(
    expected: tuple[tuple[Value, Value], ...], actual: dict[object, object]
) -> bool:
    if len(expected) != len(actual):
        return False
    for key, item in expected:
        wanted = bound(key)
        if wanted not in actual or not agrees(item, actual[wanted]):
            return False
    return True


def same_items(expected: tuple[Value, ...], actual: list[object]) -> bool:
    return len(expected) == len(actual) and all(
        agrees(item, got) for item, got in zip(expected, actual, strict=True)
    )


def agrees(expected: Value, actual: object) -> bool:
    match expected:
        case DoubleValue(value=number):
            return same_double(number, actual)
        case ListValue(values=values):
            return isinstance(actual, list) and same_items(
                values, cast(list[object], actual)
            )
        case MapValue(entries=entries):
            return isinstance(actual, dict) and same_entries(
                entries, cast(dict[object, object], actual)
            )
        case ObjectValue(type_url=type_url):
            return type_url == TIMESTAMP_TYPE_URL and isinstance(
                actual, datetime.datetime
            )
        case _:
            wanted = bound(expected)
            return type(wanted) is type(actual) and wanted == actual


def case_context(case: Case) -> dict[str, object]:
    return {name: bound(value) for name, value in case.bindings}


def run_case(binding: Binding, case: Case) -> tuple[bool, Typed]:
    context = case_context(case)
    results: list[object] = []

    def evaluate() -> object:
        result = binding.native_compile(case.expression).execute(context)
        results.append(result)
        return result

    observed = outcome(binding, evaluate)
    if isinstance(case.expected, ExpectedError):
        return observed[0] == "error", observed
    return bool(results) and agrees(case.expected.value, results[0]), observed
