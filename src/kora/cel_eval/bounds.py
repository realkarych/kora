import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from kora.cel.cost import (
    DATA_BYTES,
    DATA_DEPTH,
    DATA_ITEMS,
    DATA_STRING_BYTES,
    DATA_VALUES,
    BoundError,
    ValueBound,
    is_exact_bound,
    is_integer,
    is_plain_mapping,
)
from kora.cel.paths import JsonValue
from kora.cel_eval.errors import ErrorKind

MAX_BYTES: int = DATA_BYTES
MAX_ITEMS: int = DATA_ITEMS
MAX_STRING_BYTES: int = DATA_STRING_BYTES
MAX_DEPTH: int = DATA_DEPTH
MAX_VALUES: int = DATA_VALUES


@dataclass(frozen=True)
class Size:
    bytes: int
    values: int


def string_size(value: str) -> int | ErrorKind:
    if len(value) > MAX_STRING_BYTES:
        return ErrorKind.BOUND_EXCEEDED
    encoded = 0
    canonical = 2
    for char in value:
        point = ord(char)
        if 0xD800 <= point <= 0xDFFF:
            return ErrorKind.INVALID_VALUE
        width = 1 if point < 128 else 2 if point < 2_048 else 3 if point < 65_536 else 4
        encoded += width
        canonical += 2 if char in '"\\\b\f\n\r\t' else 6 if point < 32 else width
        if encoded > MAX_STRING_BYTES:
            return ErrorKind.BOUND_EXCEEDED
    return canonical


def scalar_size(value: object) -> int | ErrorKind:
    if value is None:
        return 4
    if type(value) is bool:
        return 4 if value else 5
    if type(value) is int:
        integer = value
        if integer.bit_length() > 1_024:
            return ErrorKind.NUMBER_OUT_OF_RANGE
        return len(str(integer))
    if type(value) is float:
        number = value
        if not math.isfinite(number):
            return ErrorKind.NUMBER_OUT_OF_RANGE
        return len(repr(number))
    if type(value) is str:
        return string_size(value)
    return ErrorKind.INVALID_VALUE


def measure(value: object) -> Size | ErrorKind:
    pending: list[tuple[object, int]] = [(value, 0)]
    size = 0
    count = 0
    while pending:
        current, depth = pending.pop()
        count += 1
        if depth > MAX_DEPTH or count > MAX_VALUES:
            return ErrorKind.BOUND_EXCEEDED
        if type(current) is list:
            sequence = cast(list[object], current)
            if len(sequence) > MAX_ITEMS:
                return ErrorKind.BOUND_EXCEEDED
            size += 2 + max(0, len(sequence) - 1)
            pending.extend((child, depth + 1) for child in sequence)
        elif type(current) is dict:
            mapping = cast(dict[object, object], current)
            if len(mapping) > MAX_ITEMS:
                return ErrorKind.BOUND_EXCEEDED
            size += 2 + max(0, len(mapping) - 1) + len(mapping)
            for key, child in mapping.items():
                if type(key) is not str:
                    return ErrorKind.INVALID_VALUE
                key_size = string_size(key)
                if isinstance(key_size, ErrorKind):
                    return key_size
                size += key_size
                if size > MAX_BYTES:
                    return ErrorKind.BOUND_EXCEEDED
                pending.append((child, depth + 1))
        else:
            scalar = scalar_size(current)
            if isinstance(scalar, ErrorKind):
                return scalar
            size += scalar
        if size > MAX_BYTES:
            return ErrorKind.BOUND_EXCEEDED
    return Size(size, count)


def checked_bound(bound: object) -> ValueBound:
    if not is_exact_bound(bound):
        raise BoundError("a bound is not a value bound")
    declared = cast(ValueBound, bound)
    sizes = (declared.items, declared.string_bytes)
    if not all(map(is_integer, sizes)) or not (
        is_plain_mapping(declared.fields)
        or isinstance(declared.fields, MappingProxyType)
    ):
        raise BoundError("a value bound is malformed")
    return declared


def admitted(value: JsonValue, bound: ValueBound) -> bool:
    pending: list[tuple[JsonValue, object]] = [(value, bound)]
    while pending:
        current, unchecked = pending.pop()
        declared = checked_bound(unchecked)
        if isinstance(current, str):
            if len(current.encode("utf-8")) > declared.string_bytes:
                return False
        elif isinstance(current, list):
            if len(current) > declared.items:
                return False
            if declared.element is not None:
                pending.extend((child, declared.element) for child in current)
        elif isinstance(current, dict):
            if len(current) > declared.items:
                return False
            for key, child in current.items():
                child_bound = declared.fields.get(key, declared.element)
                if child_bound is not None:
                    pending.append((child, child_bound))
    return True
