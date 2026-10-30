import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Never, TypeVar

Reason = Literal["unsupported_binding", "unsupported_schema", "preparation_exhausted"]
PARAMETERS: Mapping[str, int] = MappingProxyType(
    {
        "max_input_bytes": 1048576,
        "max_input_nodes": 16384,
        "max_input_depth": 32,
        "max_schema_documents": 64,
        "max_schema_positions": 2048,
        "max_reference_edges": 4096,
        "max_numeric_bits": 14000,
        "max_preparation_work": 16777216,
        "max_auxiliary_bytes": 4194304,
        "max_auxiliary_entries": 32768,
    }
)
T = TypeVar("T")
K = TypeVar("K", str, tuple[int, int])


class PreparationRefused(Exception):
    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


def refuse(reason: Reason = "unsupported_schema") -> Never:
    raise PreparationRefused(reason)


@dataclass(slots=True)
class PreparationMeter:
    work: int = 0
    auxiliary_bytes: int = 0
    auxiliary_entries: int = 0

    def __post_init__(self) -> None:
        self.reserve(0)

    def reserve(self, work: int = 1, byte_count: int = 0, entries: int = 0) -> None:
        values = (
            work,
            byte_count,
            entries,
            self.work,
            self.auxiliary_bytes,
            self.auxiliary_entries,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("invalid preparation reservation")
        if (
            self.work + work > PARAMETERS["max_preparation_work"]
            or self.auxiliary_bytes + byte_count > PARAMETERS["max_auxiliary_bytes"]
            or self.auxiliary_entries + entries > PARAMETERS["max_auxiliary_entries"]
        ):
            refuse("preparation_exhausted")
        self.work += work
        self.auxiliary_bytes += byte_count
        self.auxiliary_entries += entries

    def result(self, size: int) -> None:
        self.reserve(1 + size, size)

    def compare(self, left: str, right: str) -> int:
        self.reserve(1 + min(len(left), len(right)))
        return (left > right) - (left < right)

    def string_size(self, value: str) -> int:
        self.reserve(1 + len(value))
        size = 2
        for character in value:
            point = ord(character)
            if 0xD800 <= point <= 0xDFFF:
                refuse()
            size += (
                2
                if character in '"\\\b\f\n\r\t'
                else 6
                if point < 32
                else 1
                if point < 128
                else 2
                if point < 2048
                else 3
                if point < 65536
                else 4
            )
        return size

    def integer(self, value: int) -> str:
        self.reserve(2)
        bits = value.bit_length()
        if bits > PARAMETERS["max_numeric_bits"]:
            refuse()
        negative = value < 0
        if negative:
            self.reserve(1 + max(1, (bits + 29) // 30), (bits + 7) // 8)
            value = -value
        if not value:
            self.result(1)
            return "0"
        chunks: list[str] = []
        while value:
            bits = value.bit_length()
            self.reserve(1 + 2 * max(1, (bits + 29) // 30), (bits + 7) // 8 + 4)
            value, remainder = divmod(value, 1000000000)
            self.reserve(10, 9)
            self.reserve(entries=1)
            chunks.append(f"{remainder:09d}")
        self.result(9)
        self.reserve(1 + len(chunks))
        chunks.reverse()
        chunks[0] = chunks[0].lstrip("0")
        self.result(len(chunks[0]) + 9 * (len(chunks) - 1) + int(negative))
        if negative:
            self.reserve(1 + len(chunks), entries=1)
            chunks.insert(0, "-")
        return "".join(chunks)

    def floating(self, value: float) -> str:
        self.reserve(2)
        if not math.isfinite(value):
            refuse()
        self.reserve(4096, 24)
        spelling = repr(value)
        self.result(len(spelling))
        spelling.encode("ascii")
        return spelling


def merge_sort(rows: list[T], key: Callable[[T], K], meter: PreparationMeter) -> None:
    count = len(rows)
    levels = (max(1, count) - 1).bit_length()
    meter.reserve(1 + 2 * levels * count, entries=2 * count)
    if count:
        first = key(rows[0])
        if isinstance(first, str):
            characters = sum(
                len(value) for row in rows if isinstance(value := key(row), str)
            )
            meter.reserve(2 * levels * characters)
        else:
            meter.reserve(2 * levels * count)
    scratch = rows.copy()
    source, destination = rows, scratch
    width = 1
    while width < count:
        for start in range(0, count, 2 * width):
            left, middle, end = (
                start,
                min(start + width, count),
                min(start + 2 * width, count),
            )
            right = middle
            for index in range(start, end):
                if left < middle and (
                    right == end or key(source[left]) <= key(source[right])
                ):
                    destination[index] = source[left]
                    left += 1
                else:
                    destination[index] = source[right]
                    right += 1
        source, destination = destination, source
        width *= 2
    if source is not rows:
        for index, row in enumerate(source):
            rows[index] = row


def binary_lookup(
    rows: tuple[tuple[int, int], ...] | list[tuple[int, int]],
    query: int,
    meter: PreparationMeter,
) -> int:
    meter.reserve(1 + len(rows).bit_length())
    low, high = 0, len(rows)
    while low < high:
        middle = (low + high) // 2
        if rows[middle][0] < query:
            low = middle + 1
        else:
            high = middle
    return rows[low][1] if low < len(rows) and rows[low][0] == query else -1


def copy_input_vector(values: list[int], meter: PreparationMeter) -> tuple[int, ...]:
    meter.reserve(1 + len(values), entries=len(values))
    return tuple(values)
