from collections.abc import Mapping

from kora.cel import JsonValue
from kora.cel.types import DOUBLE, DYN, CelType, ListOf
from tests.measurements.cel_cost_workload import (
    SAMPLES,
    WARMUP,
    ConversionCase,
    binding_statistics,
)

BOUND_PAIRS: tuple[tuple[int, int], ...] = tuple(
    (size, count) for size in (1024, 2048, 4096, 8192) for count in (128, 256, 512)
)
BOUND_FAMILIES: tuple[str, ...] = (
    "ascii",
    "unicode",
    "escapes",
    "empty_lists",
    "small_maps",
    "deep_chains",
    "numbers",
    "key_heavy",
    "mixed",
    "many_variables",
)
BOUND_KEYS: frozenset[tuple[int, int, str]] = frozenset(
    (size, count, family) for size, count in BOUND_PAIRS for family in BOUND_FAMILIES
)
NATIVE_BATCH: int = 32


def conversion_samples(
    preparation: list[int],
    native_totals: list[int],
    control_totals: list[int],
    calls: int = NATIVE_BATCH,
) -> list[int]:
    if type(calls) is not int or calls not in (1, NATIVE_BATCH):
        raise ValueError("invalid native batch count")
    if any(
        len(values) != WARMUP + SAMPLES
        or any(type(value) is not int or value < 0 for value in values)
        for values in (preparation, native_totals, control_totals)
    ):
        raise ValueError("invalid conversion vectors")
    if any(
        actual <= control
        for actual, control in zip(
            native_totals[WARMUP:], control_totals[WARMUP:], strict=True
        )
    ):
        raise ValueError("raw native conversion contribution is not identifiable")
    return [
        prep + (actual + calls - 1) // calls - control // calls
        for prep, actual, control in zip(
            preparation, native_totals, control_totals, strict=True
        )
    ]


def select_bounds(
    minima: Mapping[tuple[int, int, str], int],
) -> tuple[int, int] | None:
    if frozenset(minima) != BOUND_KEYS or any(
        type(value) is not int or value < 0 for value in minima.values()
    ):
        raise ValueError("bound selection does not match its complete manifest")
    qualifying = [
        (size, count)
        for size, count in BOUND_PAIRS
        if 3 * max(minima[size, count, family] for family in BOUND_FAMILIES)
        <= 5_000_000
    ]
    return max(qualifying, key=lambda pair: (pair[0] * pair[1], *pair), default=None)


def narrow_chain(values: int) -> JsonValue:
    value: JsonValue = []
    for _ in range(values - 1):
        value = [value]
    return value


def chains(size: int, count: int, *, padded: bool) -> list[JsonValue]:
    available = count - (2 if padded else 1)
    while available:
        complete, tail = divmod(available, 32)
        needed = (4 if padded else 1) + 65 * complete + (2 * tail + 1 if tail else 0)
        if needed <= size:
            result: list[JsonValue] = [narrow_chain(32) for _ in range(complete)]
            if tail:
                result.append(narrow_chain(tail))
            if padded:
                result.insert(0, "a" * (size - needed))
            return result
        available -= 1
    raise ValueError("candidate cannot hold its chain workload")


def numeric_lists(size: int, count: int) -> list[JsonValue]:
    result: list[JsonValue] = []
    bytes_used = 2
    values_used = 1
    while values_used + 2 <= count:
        length = min(14, count - values_used - 1)
        integers = list(range(length))
        converted: list[JsonValue] = [float(value) for value in integers]
        added = binding_statistics({"item": converted})[0] + bool(result)
        if bytes_used + added > size:
            if length == 1:
                break
            length -= 1
            while length:
                integers = list(range(length))
                converted = [float(value) for value in integers]
                added = binding_statistics({"item": converted})[0] + bool(result)
                if bytes_used + added <= size:
                    break
                length -= 1
            if not length:
                break
        integer_values: list[JsonValue] = [value for value in integers]
        result.append(integer_values)
        bytes_used += added
        values_used += length + 1
    return result


def bound_case(size: int, count: int, family: str) -> ConversionCase:
    if (size, count, family) not in BOUND_KEYS:
        raise ValueError("unknown bound workload identity")
    kind: CelType = DYN
    value: list[JsonValue]
    constraint: str
    if family in {"ascii", "unicode", "escapes"}:
        character, width = {
            "ascii": ("a", 1),
            "unicode": ("\U0001f642", 4),
            "escapes": ("\x00", 6),
        }[family]
        units, remainder = divmod((size - 13) // width, 4)
        value = [character * (units + (index < remainder)) for index in range(4)]
        constraint = "four_string_values_and_character_width"
    elif family == "empty_lists":
        value = [[] for _ in range(min(count - 1, (size - 1) // 3))]
        constraint = "empty_list_separator_bytes_or_value_count"
    elif family == "small_maps":
        value = [
            {f"k{index}": None for index in range(6)}
            for _ in range(min((count - 1) // 7, (size - 1) // 62))
        ]
        constraint = "six_key_map_units"
    elif family in {"deep_chains", "mixed"}:
        value = chains(size, count, padded=family == "mixed")
        constraint = "depth_32_chain_units_and_value_count"
    elif family == "numbers":
        value = numeric_lists(size, count)
        kind = ListOf(ListOf(DOUBLE))
        constraint = "converted_double_bytes_or_value_count"
    elif family == "key_heavy":
        entries = min(count - 2, (size - 3) // 12)
        mapping: dict[str, JsonValue] = {
            f"k{index:03}": None for index in range(entries)
        }
        padding = size - (12 * entries + 3)
        last = f"k{entries - 1:03}"
        del mapping[last]
        mapping[last + "a" * padding] = None
        value = [mapping]
        constraint = "canonical_key_bytes_or_value_count"
    else:
        variables = min(count // 2, size // 4)
        padding, remainder = divmod(size - 4 * variables, variables)
        bindings: dict[str, JsonValue] = {
            f"v{index}": ["a" * (padding + (index < remainder))]
            for index in range(variables)
        }
        operations = ",".join(f"{name}.size()" for name in bindings)
        expected = ",".join("1" for _ in bindings)
        return ConversionCase(
            f"{family}-{size}-{count}",
            f"[{operations}] == [{expected}]",
            bindings,
            {name: DYN for name in bindings},
            {name: [] for name in bindings},
            NATIVE_BATCH,
            size,
            count,
            ("separate_referenced_roots_and_two_values_per_root",),
        )
    source = f"values.size() == {len(value)}"
    if family == "numbers":
        source += " && values[0][0] == 0.0"
    return ConversionCase(
        f"{family}-{size}-{count}",
        source,
        {"values": value},
        {"values": kind},
        {"values": []},
        NATIVE_BATCH,
        size,
        count,
        (constraint,),
    )


def case_by_name(name: str) -> ConversionCase:
    family, size, count = name.rsplit("-", 2)
    return bound_case(int(size), int(count), family)
