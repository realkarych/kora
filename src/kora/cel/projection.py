import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from kora.cel.characters import IDENTIFIER_PART, IDENTIFIER_START
from kora.cel.cost import (
    DATA_BYTES,
    DATA_DEPTH,
    DATA_ITEMS,
    DATA_STRING_BYTES,
    DATA_VALUES,
    BoundError,
)
from kora.cel.diagnostics import Reason, fail
from kora.cel.limits import MAX_CODE_POINTS
from kora.cel.meter import Meter
from kora.cel.paths import Field, JsonValue
from kora.cel.render import render_string
from kora.cel.reserved import RESERVED_IDENTIFIERS
from kora.cel.tokens import KEYWORDS, TokenKind, lex


@dataclass(frozen=True)
class Key:
    name: str


class Element(Enum):
    ALL = "*"


ProjectionStep = Field | Key | Element
INTEGER_LIMIT: Final[int] = 10**DATA_BYTES
NEGATIVE_INTEGER_LIMIT: Final[int] = -INTEGER_LIMIT


def parse_projection_path(
    text: str, meter: Meter | None = None
) -> tuple[ProjectionStep, ...]:
    used = Meter() if meter is None else meter
    if len(text) > 16 * MAX_CODE_POINTS:
        raise fail(Reason.LENGTH, 16 * MAX_CODE_POINTS)
    tokens = lex(text, used)
    position = 0
    steps: list[ProjectionStep] = []
    while tokens[position].kind is not TokenKind.END:
        token = tokens[position]
        position += 1
        if token.kind is TokenKind.DOT:
            name = tokens[position]
            if name.kind is not TokenKind.IDENTIFIER:
                raise fail(Reason.INVALID_PATH, name.offset)
            steps.append(Field(str(name.value)))
            position += 1
        elif token.kind is TokenKind.LEFT_BRACKET:
            key = tokens[position]
            if key.kind is TokenKind.STAR:
                steps.append(Element.ALL)
            elif key.kind is TokenKind.STRING and isinstance(key.value, str):
                steps.append(Key(key.value))
            else:
                raise fail(Reason.INVALID_PATH, key.offset)
            position += 1
            if tokens[position].kind is not TokenKind.RIGHT_BRACKET:
                raise fail(Reason.INVALID_PATH, tokens[position].offset)
            position += 1
        else:
            raise fail(Reason.INVALID_PATH, token.offset)
        if len(steps) > DATA_DEPTH:
            raise fail(Reason.DEPTH, token.offset)
    return tuple(steps)


def render_projection_step(step: ProjectionStep) -> str:
    if isinstance(step, Element):
        return "[*]"
    name = step.name
    if (
        isinstance(step, Field)
        and name
        and name[0] in IDENTIFIER_START
        and all(character in IDENTIFIER_PART for character in name)
        and name not in KEYWORDS
        and name not in RESERVED_IDENTIFIERS
    ):
        return "." + name
    quoted = render_string(name)[1:-1].replace('\\"', '"').replace("'", "\\'")
    return "['" + quoted + "']"


def render_projection_path(
    steps: tuple[ProjectionStep, ...], meter: Meter | None = None
) -> str:
    parts = [render_projection_step(step) for step in steps]
    if meter is not None:
        meter.tick(sum(map(len, parts)))
    return "".join(parts)


@dataclass
class Trie:
    whole: bool = False
    presence: bool = False
    keyed: bool = False
    fields: dict[str, "Trie"] = field(default_factory=lambda: {})
    element: "Trie | None" = None

    def insert(
        self, steps: tuple[ProjectionStep, ...], whole: bool, meter: Meter
    ) -> None:
        current = self
        for step in steps:
            if current.whole:
                return
            if isinstance(step, Element):
                meter.tick()
                if current.element is None:
                    current.element = Trie()
                current = current.element
            else:
                meter.tick(1 + len(step.name))
                current = current.fields.setdefault(step.name, Trie())
                current.keyed |= isinstance(step, Key)
        if whole:
            current.whole = True
            current.presence = False
            current.fields.clear()
            current.element = None
        elif not current.whole:
            current.presence = True


def ascii_sorted(values: list[str], meter: Meter) -> tuple[str, ...]:
    result: list[str] = []
    pending = [(values, 0)]
    while pending:
        group, position = pending.pop()
        if len(group) <= 1:
            result.extend(group)
            continue
        buckets: dict[int, list[str]] = {}
        for value in group:
            meter.tick()
            if position == len(value):
                result.append(value)
            else:
                buckets.setdefault(ord(value[position]), []).append(value)
        meter.tick(8 * len(buckets))
        pending.extend(
            (buckets[key], position + 1) for key in sorted(buckets, reverse=True)
        )
    return tuple(result)


def freeze(root: Trie, meter: Meter) -> tuple[tuple[str, ...], tuple[str, ...]]:
    whole: list[str] = []
    presence: list[str] = []
    pending = [(root, "")]
    while pending:
        node, path = pending.pop()
        meter.tick()
        if node.whole:
            whole.append(path)
            continue
        if node.presence:
            presence.append(path)
        for name, child in node.fields.items():
            step = Key(name) if child.keyed else Field(name)
            rendered = render_projection_step(step)
            meter.tick(len(path) + len(rendered))
            pending.append((child, path + rendered))
        if node.element is not None:
            meter.tick(len(path) + 3)
            pending.append((node.element, path + "[*]"))
    return ascii_sorted(whole, meter), ascii_sorted(presence, meter)


@dataclass(frozen=True)
class PathSet:
    whole: tuple[str, ...] = ()
    presence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        meter = Meter()
        root = self.trie(meter)
        whole, presence = freeze(root, meter)
        object.__setattr__(self, "whole", whole)
        object.__setattr__(self, "presence", presence)

    @classmethod
    def from_trie(cls, root: Trie, meter: Meter) -> "PathSet":
        whole, presence = freeze(root, meter)
        result = object.__new__(cls)
        object.__setattr__(result, "whole", whole)
        object.__setattr__(result, "presence", presence)
        return result

    def trie(self, meter: Meter) -> Trie:
        root = Trie()
        for whole, paths in ((True, self.whole), (False, self.presence)):
            for path in paths:
                root.insert(parse_projection_path(path, meter), whole, meter)
        return root


def projection_bytes(projections: Mapping[str, PathSet]) -> int:
    if not projections:
        return 0
    return 256 + sum(
        256
        + 8 * len(name)
        + sum(128 + 8 * len(path) for path in (*paths.whole, *paths.presence))
        for name, paths in projections.items()
    )


def empty(value: JsonValue) -> JsonValue:
    if type(value) is dict:
        return {}
    if type(value) is list:
        return []
    if type(value) is str:
        return ""
    if type(value) is bool or value is None:
        return value
    if type(value) is int:
        return 0
    if type(value) is float:
        return 0.0
    raise BoundError("a projection value is not JSON")


def string_bytes(value: str, meter: Meter) -> int:
    if len(value) > DATA_STRING_BYTES:
        raise BoundError("a projection exceeds the data bounds")
    meter.tick(len(value))
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise BoundError("a projection value is not JSON")
    encoded = value.encode("utf-8")
    if len(encoded) > DATA_STRING_BYTES:
        raise BoundError("a projection exceeds the data bounds")
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def scalar_bytes(value: JsonValue, meter: Meter) -> int:
    if type(value) is str:
        return string_bytes(value, meter)
    if type(value) is bool:
        return 4 if value else 5
    if value is None:
        return 4
    if type(value) is int:
        meter.tick(min(value.bit_length(), INTEGER_LIMIT.bit_length()))
        if value >= INTEGER_LIMIT or value <= NEGATIVE_INTEGER_LIMIT:
            raise BoundError("a projection exceeds the data bounds")
        size = len(str(value))
        meter.tick(size)
        return size
    if type(value) is float and math.isfinite(value):
        return len(repr(value))
    raise BoundError("a projection value is not JSON")


Child = tuple[str | int, JsonValue, Trie]


def children(value: JsonValue, plan: Trie, meter: Meter) -> Iterator[Child]:
    if type(value) is dict:
        if plan.whole:
            if len(value) > DATA_ITEMS:
                raise BoundError("a projection exceeds the data bounds")
            for name, child in value.items():
                if type(name) is not str:
                    raise BoundError("a projection key is not a string")
                yield name, child, plan
        else:
            for name, selected in plan.fields.items():
                meter.tick(1 + len(name))
                if name in value:
                    yield name, value[name], selected
    elif type(value) is list and (plan.whole or plan.element is not None):
        if len(value) > DATA_ITEMS:
            raise BoundError("a projection exceeds the data bounds")
        selected = plan if plan.whole else plan.element
        if selected is not None:
            for index, child in enumerate(value):
                yield index, child, selected


@dataclass
class Frame:
    identity: int
    target: dict[str, JsonValue] | list[JsonValue]
    children: Iterator[Child]
    depth: int


class Cutter:
    def __init__(self, meter: Meter) -> None:
        self.meter: Meter = meter
        self.bytes: int = 0
        self.values: int = 0
        self.frames: list[Frame] = []
        self.ancestors: set[int] = set()

    def charge(self, size: int) -> None:
        self.bytes += size
        if self.bytes > DATA_BYTES:
            raise BoundError("a projection exceeds the data bounds")

    def enter(self, value: JsonValue, plan: Trie, depth: int) -> JsonValue:
        self.meter.tick()
        self.values += 1
        if depth > DATA_DEPTH or self.values > DATA_VALUES:
            raise BoundError("a projection exceeds the data bounds")
        target = empty(value)
        if type(value) is dict or type(value) is list:
            self.charge(2)
            if not isinstance(target, dict | list):
                raise BoundError("a projection value is not JSON")
            if plan.whole or plan.fields or plan.element is not None:
                if id(value) in self.ancestors:
                    raise BoundError("a projection contains a cycle")
                self.ancestors.add(id(value))
                self.frames.append(
                    Frame(id(value), target, children(value, plan, self.meter), depth)
                )
        else:
            target = value if plan.whole else target
            self.charge(scalar_bytes(target, self.meter))
        return target

    def cut(self, value: JsonValue, plan: Trie) -> JsonValue:
        result = self.enter(value, plan, 0)
        while self.frames:
            frame = self.frames[-1]
            child = next(frame.children, None)
            if child is None:
                self.ancestors.remove(frame.identity)
                self.frames.pop()
                continue
            key, original, selected = child
            target = frame.target
            if isinstance(target, dict) and isinstance(key, str):
                self.charge(string_bytes(key, self.meter) + 1 + bool(target))
                target[key] = self.enter(original, selected, frame.depth + 1)
            elif isinstance(target, list) and isinstance(key, int):
                self.charge(int(bool(target)))
                target.append(self.enter(original, selected, frame.depth + 1))
            else:
                raise BoundError("a projection step does not match its container")
        return result


def cut(value: JsonValue, path_set: PathSet, meter: Meter | None = None) -> JsonValue:
    used = Meter() if meter is None else meter
    return Cutter(used).cut(value, path_set.trie(used))
