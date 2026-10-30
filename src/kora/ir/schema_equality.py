from collections.abc import Iterator
from dataclasses import dataclass

from kora.ir.json import JsonObject, JsonValue


@dataclass(frozen=True)
class JsonComparison:
    equal: bool
    values_checked: int


def object_pairs(
    left: JsonObject, right: JsonObject
) -> Iterator[tuple[JsonValue, JsonValue]]:
    for key, value in left.items():
        yield value, right[key]


def compare_json(left: JsonValue, right: JsonValue) -> JsonComparison:
    pending: list[Iterator[tuple[JsonValue, JsonValue]]] = [iter(((left, right),))]
    count = 0
    while pending:
        pair = next(pending[-1], None)
        if pair is None:
            pending.pop()
            continue
        count += 1
        left, right = pair
        if isinstance(left, dict):
            if (
                not isinstance(right, dict)
                or len(left) != len(right)
                or not all(key in right for key in left)
            ):
                return JsonComparison(False, count)
            pending.append(object_pairs(left, right))
        elif isinstance(left, list):
            if not isinstance(right, list) or len(left) != len(right):
                return JsonComparison(False, count)
            pending.append(zip(left, right, strict=True))
        elif isinstance(right, dict | list):
            return JsonComparison(False, count)
        elif isinstance(left, bool) or isinstance(right, bool):
            if left is not right:
                return JsonComparison(False, count)
        elif isinstance(left, int | float) and isinstance(right, int | float):
            if left != right:
                return JsonComparison(False, count)
        elif type(left) is not type(right) or left != right:
            return JsonComparison(False, count)
    return JsonComparison(True, count)
