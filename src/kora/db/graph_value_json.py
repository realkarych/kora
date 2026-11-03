import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from json.encoder import encode_basestring_ascii
from typing import cast

from pydantic_core import to_json

from kora.db.types import JsonObject, JsonValue

NUMBER: re.Pattern[str] = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
)


class GraphValueFailure(StrEnum):
    BUDGET = "graph value byte budget exceeded"
    INVALID = "invalid graph value representation"
    VERSION = "unsupported graph value encoding"


class GraphValueRejected(ValueError):
    def __init__(self, reason: GraphValueFailure) -> None:
        self.reason: GraphValueFailure = reason
        super().__init__(reason.value)


@dataclass
class TextBudget:
    remaining: int

    def __post_init__(self) -> None:
        if type(self.remaining) is not int or self.remaining <= 0:
            raise GraphValueRejected(GraphValueFailure.BUDGET)

    def reserve(self, count: int) -> None:
        if count > self.remaining:
            raise GraphValueRejected(GraphValueFailure.BUDGET)
        self.remaining -= count


def quoted(value: str, budget: TextBudget) -> str:
    budget.reserve(2)
    for character in value:
        point = ord(character)
        size = (
            2
            if character in '"\\\b\f\n\r\t'
            else 6
            if point < 32 or 127 <= point <= 65535
            else 12
            if point > 65535
            else 1
        )
        budget.reserve(size)
    return encode_basestring_ascii(value)


def scalar_text(value: object, budget: TextBudget) -> str:
    if type(value) is str:
        return quoted(value, budget)
    if type(value) is int:
        integer = value
        available = budget.remaining - (integer < 0)
        bits = integer.bit_length()
        if available < 1 or bits > 4 * available:
            raise GraphValueRejected(GraphValueFailure.BUDGET)
        if (bits * 30103) // 100000 + 1 > available and abs(integer) >= 10**available:
            raise GraphValueRejected(GraphValueFailure.BUDGET)
        rendered = to_json(integer).decode("ascii")
    elif value is None:
        rendered = "null"
    elif type(value) is bool:
        rendered = "true" if value else "false"
    elif type(value) is float and math.isfinite(value):
        rendered = json.dumps(value, allow_nan=False, separators=(",", ":"))
    else:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    budget.reserve(len(rendered))
    return rendered


@dataclass
class WriteFrame:
    identity: int
    entries: Iterator[tuple[object, object]]
    ending: str
    names: set[str] = field(default_factory=set[str])
    first: bool = True


def write_frame(value: object, budget: TextBudget) -> WriteFrame:
    budget.reserve(2)
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        return WriteFrame(id(mapping), iter(mapping.items()), "}")
    sequence = cast(list[object], value)
    return WriteFrame(id(value), iter(enumerate(sequence)), "]")


def render_object(
    value: object, budget: TextBudget, *, reject_aliases: bool = False
) -> str:
    if type(value) is not dict:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    root = cast(dict[object, object], value)
    frames = [write_frame(root, budget)]
    active = {id(root)}
    fragments = ["{"]
    while frames:
        frame = frames[-1]
        entry = next(frame.entries, None)
        if entry is None:
            fragments.append(frame.ending)
            if not reject_aliases:
                active.remove(frame.identity)
            frames.pop()
            continue
        if not frame.first:
            budget.reserve(1)
            fragments.append(",")
        frame.first = False
        key, member = entry
        if frame.ending == "}":
            if type(key) is not str:
                raise GraphValueRejected(GraphValueFailure.INVALID)
            budget.reserve(1)
            name = quoted(key, budget)
            decoded, _ = decoded_string(name, 0)
            if decoded in frame.names:
                raise GraphValueRejected(GraphValueFailure.INVALID)
            frame.names.add(decoded)
            fragments.extend((name, ":"))
        if type(member) in (dict, list):
            if id(member) in active:
                raise GraphValueRejected(GraphValueFailure.INVALID)
            child = write_frame(member, budget)
            frames.append(child)
            active.add(child.identity)
            fragments.append("{" if child.ending == "}" else "[")
        else:
            fragments.append(scalar_text(member, budget))
    return "".join(fragments)


def integer_value(token: str) -> int:
    start = int(token.startswith("-"))
    value = 0
    for offset in range(start, len(token), 9):
        chunk = token[offset : offset + 9]
        value = value * 10 ** len(chunk) + int(chunk)
    return -value if start else value


def decoded_string(text: str, position: int) -> tuple[str, int]:
    if not text.startswith('"', position):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    try:
        parsed: object
        parsed, end = json.JSONDecoder().raw_decode(text, position)
    except json.JSONDecodeError:
        raise GraphValueRejected(GraphValueFailure.INVALID) from None
    if not isinstance(parsed, str):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return parsed, end


@dataclass
class ReadFrame:
    container: JsonObject | list[JsonValue]
    first: bool = True


@dataclass
class ObjectReader:
    text: str
    position: int = 0

    def take(self, expected: str) -> None:
        if not self.text.startswith(expected, self.position):
            raise GraphValueRejected(GraphValueFailure.INVALID)
        self.position += len(expected)

    def string(self) -> str:
        value, self.position = decoded_string(self.text, self.position)
        return value

    def value(self) -> JsonValue:
        character = self.text[self.position : self.position + 1]
        if character == '"':
            return self.string()
        if character == "{":
            self.position += 1
            return {}
        if character == "[":
            self.position += 1
            return []
        for token, scalar in (("null", None), ("true", True), ("false", False)):
            if self.text.startswith(token, self.position):
                self.position += len(token)
                return scalar
        match = NUMBER.match(self.text, self.position)
        if match is None:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        token = match.group()
        self.position = match.end()
        if "." not in token and "e" not in token and "E" not in token:
            return integer_value(token)
        number = float(token)
        if not math.isfinite(number):
            raise GraphValueRejected(GraphValueFailure.INVALID)
        return number

    def object(self) -> JsonObject:
        self.take("{")
        root: JsonObject = {}
        frames = [ReadFrame(root)]
        while frames:
            frame = frames[-1]
            container = frame.container
            ending = "}" if isinstance(container, dict) else "]"
            if self.text.startswith(ending, self.position):
                self.position += 1
                frames.pop()
                continue
            if not frame.first:
                self.take(",")
            frame.first = False
            if isinstance(container, dict):
                key = self.string()
                if key in container:
                    raise GraphValueRejected(GraphValueFailure.INVALID)
                self.take(":")
                value = self.value()
                container[key] = value
            else:
                value = self.value()
                container.append(value)
            if isinstance(value, dict | list):
                frames.append(ReadFrame(value))
        if self.position != len(self.text):
            raise GraphValueRejected(GraphValueFailure.INVALID)
        return root
