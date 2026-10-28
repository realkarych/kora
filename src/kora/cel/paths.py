from dataclasses import dataclass
from typing import Final, TypeAlias

from kora.cel.diagnostics import Reason, fail
from kora.cel.limits import MAX_CODE_POINTS, MAX_DEPTH, MAX_INT
from kora.cel.meter import Meter
from kora.cel.reserved import RESERVED_IDENTIFIERS
from kora.cel.tokens import Token, TokenKind, lex

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)


@dataclass(frozen=True)
class Field:
    name: str


@dataclass(frozen=True)
class Position:
    index: int


PathStep = Field | Position


@dataclass(frozen=True)
class Path:
    steps: tuple[PathStep, ...]


@dataclass(frozen=True)
class Absent:
    pass


ABSENT: Final[Absent] = Absent()

HEX_PREFIXES: Final[tuple[str, ...]] = ("0x",)


class PathParser:
    def __init__(self, text: str, tokens: list[Token]) -> None:
        self.text: str = text
        self.tokens: list[Token] = tokens
        self.position: int = 0

    def current(self) -> Token:
        return self.tokens[self.position]

    def advance(self) -> Token:
        token = self.tokens[self.position]
        if token.kind is not TokenKind.END:
            self.position += 1
        return token

    def parse(self) -> Path:
        first = self.advance()
        if first.kind is not TokenKind.IDENTIFIER:
            raise fail(Reason.INVALID_PATH, first.offset)
        if first.value in RESERVED_IDENTIFIERS:
            raise fail(Reason.RESERVED_IDENTIFIER, first.offset)
        steps: list[PathStep] = [Field(str(first.value))]
        while self.current().kind is not TokenKind.END:
            steps.append(self.step())
            if len(steps) - 1 > MAX_DEPTH:
                raise fail(Reason.DEPTH, self.tokens[self.position - 1].offset)
        return Path(tuple(steps))

    def step(self) -> PathStep:
        token = self.advance()
        if token.kind is TokenKind.DOT:
            name = self.advance()
            if name.kind is not TokenKind.IDENTIFIER:
                raise fail(Reason.INVALID_PATH, name.offset)
            return Field(str(name.value))
        if token.kind is not TokenKind.LEFT_BRACKET:
            raise fail(Reason.INVALID_PATH, token.offset)
        key = self.advance()
        if self.advance().kind is not TokenKind.RIGHT_BRACKET:
            raise fail(Reason.INVALID_PATH, key.offset)
        if key.kind is TokenKind.STRING and isinstance(key.value, str):
            return Field(key.value)
        is_decimal = not self.text.startswith(HEX_PREFIXES, key.offset)
        if key.kind is TokenKind.INT and isinstance(key.value, int) and is_decimal:
            if key.value > MAX_INT:
                raise fail(Reason.INVALID_LITERAL, key.offset)
            return Position(key.value)
        raise fail(Reason.INVALID_PATH, key.offset)


def parse_path(text: str) -> Path:
    if len(text) > MAX_CODE_POINTS:
        raise fail(Reason.LENGTH, MAX_CODE_POINTS)
    tokens = lex(text, Meter())
    return PathParser(text, tokens).parse()


def resolve(value: JsonValue, steps: tuple[PathStep, ...]) -> JsonValue | Absent:
    current: JsonValue = value
    for step in steps:
        if isinstance(step, Field):
            if type(current) is not dict:
                return ABSENT
            if step.name not in current:
                return ABSENT
            current = current[step.name]
        else:
            if type(current) is not list or type(step.index) is not int:
                return ABSENT
            if not 0 <= step.index < len(current):
                return ABSENT
            current = current[step.index]
    return current
