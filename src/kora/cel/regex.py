import string
from dataclasses import dataclass
from typing import Final

from kora.cel.characters import DIGITS, HEX_DIGITS
from kora.cel.diagnostics import Reason
from kora.cel.limits import MAX_CODE_POINT, MAX_PATTERN_NESTING
from kora.cel.meter import Meter

LITERAL_WEIGHT: Final[int] = 32
CASELESS_LITERAL_WEIGHT: Final[int] = 80
RANGE_WEIGHT: Final[int] = 72
CASELESS_RANGE_WEIGHT: Final[int] = 304
ANY_WEIGHT: Final[int] = 1001
ANCHOR_WEIGHT: Final[int] = 32
REPETITION_OVERHEAD: Final[int] = 32
PATTERN_BASE_WEIGHT: Final[int] = 1
SYNTAX_CHARACTER_WEIGHT: Final[int] = 1
PATTERN_SIZE_CAP: Final[int] = 1_048_576

CLASS_WEIGHTS: Final[dict[str, int]] = {
    "d": 5125,
    "D": 12757,
    "w": 50172,
    "W": 46604,
    "s": 681,
    "S": 1925,
}

UNICODE_CLASS_WEIGHTS: Final[dict[str, int]] = {
    "L": 42975,
    "Lu": 26547,
    "Ll": 27667,
    "N": 9764,
    "Nd": 5125,
    "P": 11916,
    "S": 15840,
    "Z": 608,
    "Greek": 2247,
    "Latin": 2850,
    "Han": 3629,
    "Cyrillic": 1245,
}

CONTROL_ESCAPES: Final[dict[str, str]] = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "f": "\f",
    "v": "\v",
    "a": "\a",
}

ALLOWED_FLAGS: Final[frozenset[str]] = frozenset("ims")
KNOWN_FLAGS: Final[frozenset[str]] = frozenset("imsxuUR")
ANCHOR_ESCAPES: Final[frozenset[str]] = frozenset("bBAz")
SET_OPERATORS: Final[tuple[str, ...]] = ("&&", "--", "~~")
PUNCTUATION: Final[frozenset[str]] = frozenset(string.punctuation) - {"<", ">"}
NAME_START: Final[frozenset[str]] = frozenset(string.ascii_letters + "_")
NAME_CHARACTERS: Final[frozenset[str]] = NAME_START | frozenset(string.digits)
WIDE_RANGE_START: Final[int] = 0x80


class PatternError(Exception):
    def __init__(self, reason: Reason) -> None:
        super().__init__(reason.value)
        self.reason: Reason = reason


@dataclass(frozen=True)
class Escaped:
    weight: int
    literal: str | None
    repeatable: bool


def saturated(value: int) -> int:
    return min(value, PATTERN_SIZE_CAP + 1)


def is_caseless(text: str) -> bool:
    position = text.find("(?")
    while position >= 0:
        cursor = position + 2
        while cursor < len(text) and text[cursor] in KNOWN_FLAGS | {"-"}:
            if text[cursor] == "i":
                return True
            cursor += 1
        position = text.find("(?", position + 2)
    return False


class PatternParser:
    def __init__(self, text: str, meter: Meter) -> None:
        self.text: str = text
        self.position: int = 0
        self.depth: int = 0
        self.meter: Meter = meter
        self.group_names: set[str] = set()
        caseless = is_caseless(text)
        self.literal_weight: int = (
            CASELESS_LITERAL_WEIGHT if caseless else LITERAL_WEIGHT
        )
        self.range_weight: int = CASELESS_RANGE_WEIGHT if caseless else RANGE_WEIGHT

    def peek(self, ahead: int = 0) -> str:
        index = self.position + ahead
        return self.text[index] if index < len(self.text) else ""

    def advance(self) -> str:
        if self.position >= len(self.text):
            raise PatternError(Reason.INVALID_PATTERN)
        character = self.text[self.position]
        self.position += 1
        self.meter.tick()
        return character

    def nest(self) -> None:
        if self.depth + 1 > MAX_PATTERN_NESTING:
            raise PatternError(Reason.PATTERN_NESTING)
        self.depth += 1

    def parse(self) -> int:
        weight = self.alternation()
        if self.position < len(self.text):
            raise PatternError(Reason.INVALID_PATTERN)
        return saturated(
            PATTERN_BASE_WEIGHT + max(weight, len(self.text) * SYNTAX_CHARACTER_WEIGHT)
        )

    def alternation(self) -> int:
        total = self.concatenation()
        while self.peek() == "|":
            self.advance()
            total = saturated(total + self.concatenation())
        return total

    def concatenation(self) -> int:
        total = 0
        while self.position < len(self.text) and self.peek() not in ("|", ")"):
            total = saturated(total + self.repetition())
        return total

    def repetition(self) -> int:
        weight, repeatable = self.atom()
        while self.peek() in ("*", "+", "?", "{"):
            if not repeatable:
                raise PatternError(Reason.UNSUPPORTED_PATTERN)
            factor = self.counted() if self.peek() == "{" else self.simple_quantifier()
            weight = saturated(weight * factor + REPETITION_OVERHEAD)
            if self.peek() == "?":
                self.advance()
        return weight

    def simple_quantifier(self) -> int:
        return 2 if self.advance() == "+" else 1

    def number(self) -> int:
        start = self.position
        while self.peek() in DIGITS:
            self.advance()
        if self.position == start:
            raise PatternError(Reason.INVALID_PATTERN)
        return int(self.text[start : self.position])

    def counted(self) -> int:
        self.advance()
        minimum = self.number()
        maximum: int | None = minimum
        if self.peek() == ",":
            self.advance()
            maximum = None if self.peek() == "}" else self.number()
        if self.advance() != "}":
            raise PatternError(Reason.INVALID_PATTERN)
        if maximum is None:
            return max(minimum + 1, 1)
        if maximum < minimum:
            raise PatternError(Reason.INVALID_PATTERN)
        return max(maximum, 1)

    def atom(self) -> tuple[int, bool]:
        character = self.advance()
        if character == "(":
            return self.group()
        if character == "[":
            return self.bracket(), True
        if character == "\\":
            escaped = self.escape(in_class=False)
            return escaped.weight, escaped.repeatable
        if character == ".":
            return ANY_WEIGHT, True
        if character in ("^", "$"):
            return ANCHOR_WEIGHT, False
        if character in ("*", "+", "?", "{"):
            raise PatternError(Reason.INVALID_PATTERN)
        return self.literal_weight, True

    def group(self) -> tuple[int, bool]:
        if self.peek() == "?":
            self.advance()
            marker = self.peek()
            if marker == ":":
                self.advance()
            elif marker == "P" and self.peek(1) == "<":
                self.advance()
                self.advance()
                self.group_name()
            elif marker == "<" and self.peek(1) not in ("=", "!"):
                self.advance()
                self.group_name()
            elif marker in ("=", "!", "<"):
                raise PatternError(Reason.UNSUPPORTED_PATTERN)
            elif self.flags():
                return 0, False
        self.nest()
        weight = self.alternation()
        self.depth -= 1
        if self.advance() != ")":
            raise PatternError(Reason.INVALID_PATTERN)
        return weight, True

    def group_name(self) -> None:
        start = self.position
        if self.peek() not in NAME_START:
            raise PatternError(Reason.INVALID_PATTERN)
        while self.peek() in NAME_CHARACTERS:
            self.advance()
        name = self.text[start : self.position]
        if self.position == start or self.advance() != ">" or name in self.group_names:
            raise PatternError(Reason.INVALID_PATTERN)
        self.group_names.add(name)

    def flags(self) -> bool:
        seen = False
        negated = False
        pending = False
        while True:
            character = self.advance()
            if character in (")", ":"):
                if not seen or pending:
                    raise PatternError(Reason.INVALID_PATTERN)
                return character == ")"
            if character == "-":
                if negated:
                    raise PatternError(Reason.INVALID_PATTERN)
                negated = True
                pending = True
                continue
            pending = False
            if character in KNOWN_FLAGS and character not in ALLOWED_FLAGS:
                raise PatternError(Reason.UNSUPPORTED_PATTERN)
            if character not in ALLOWED_FLAGS:
                raise PatternError(Reason.INVALID_PATTERN)
            seen = True

    def bracket(self) -> int:
        self.nest()
        negated = self.peek() == "^"
        if negated:
            self.advance()
        total = 0
        first = True
        while True:
            if self.position >= len(self.text):
                raise PatternError(Reason.INVALID_PATTERN)
            if self.peek() == "]" and not first:
                self.advance()
                break
            total = saturated(total + self.class_member())
            first = False
        self.depth -= 1
        return max(total, ANY_WEIGHT) if negated else total

    def class_member(self) -> int:
        if self.text.startswith(SET_OPERATORS, self.position):
            raise PatternError(Reason.UNSUPPORTED_PATTERN)
        character = self.advance()
        if character == "[":
            if self.peek() == ":":
                raise PatternError(Reason.UNSUPPORTED_PATTERN)
            return self.bracket()
        if character == "\\":
            escaped = self.escape(in_class=True)
            if escaped.literal is None:
                if self.peek() == "-" and self.peek(1) not in ("]", ""):
                    raise PatternError(Reason.INVALID_PATTERN)
                return escaped.weight
            start = escaped.literal
        else:
            start = character
        if self.text.startswith(SET_OPERATORS, self.position):
            raise PatternError(Reason.UNSUPPORTED_PATTERN)
        if self.peek() == "-" and self.peek(1) not in ("]", ""):
            self.advance()
            end = self.range_end()
            if ord(end) < ord(start):
                raise PatternError(Reason.INVALID_PATTERN)
            if ord(end) >= WIDE_RANGE_START:
                return max(self.range_weight, ANY_WEIGHT)
            return self.range_weight
        return self.literal_weight

    def range_end(self) -> str:
        character = self.advance()
        if character == "\\":
            escaped = self.escape(in_class=True)
            if escaped.literal is None:
                raise PatternError(Reason.INVALID_PATTERN)
            return escaped.literal
        if character == "[":
            raise PatternError(Reason.INVALID_PATTERN)
        return character

    def escape(self, in_class: bool) -> Escaped:
        character = self.advance()
        if character in CLASS_WEIGHTS:
            return Escaped(CLASS_WEIGHTS[character], None, True)
        if character == "p":
            return Escaped(self.unicode_class(), None, True)
        if character == "P":
            return Escaped(max(self.unicode_class(), ANY_WEIGHT), None, True)
        if character in ANCHOR_ESCAPES:
            if in_class:
                raise PatternError(Reason.INVALID_PATTERN)
            return Escaped(ANCHOR_WEIGHT, None, False)
        if character in CONTROL_ESCAPES:
            return Escaped(self.literal_weight, CONTROL_ESCAPES[character], True)
        if character == "x":
            return Escaped(self.literal_weight, self.hex_escape(), True)
        if character in PUNCTUATION:
            return Escaped(self.literal_weight, character, True)
        raise PatternError(Reason.UNSUPPORTED_PATTERN)

    def unicode_class(self) -> int:
        if self.peek() == "{":
            self.advance()
            start = self.position
            while self.peek() not in ("}", ""):
                self.advance()
            name = self.text[start : self.position]
            self.advance()
        else:
            name = self.advance()
        weight = UNICODE_CLASS_WEIGHTS.get(name)
        if weight is None:
            raise PatternError(Reason.UNSUPPORTED_PATTERN)
        return weight

    def hex_escape(self) -> str:
        if self.peek() == "{":
            self.advance()
            start = self.position
            while self.peek() in HEX_DIGITS:
                self.advance()
            digits = self.text[start : self.position]
            if not digits or len(digits) > 8 or self.advance() != "}":
                raise PatternError(Reason.INVALID_PATTERN)
        else:
            digits = self.advance() + self.advance()
            if any(digit not in HEX_DIGITS for digit in digits):
                raise PatternError(Reason.INVALID_PATTERN)
        point = int(digits, 16)
        if point > MAX_CODE_POINT or 0xD800 <= point <= 0xDFFF:
            raise PatternError(Reason.INVALID_PATTERN)
        return chr(point)


def parse_pattern(text: str, meter: Meter | None = None) -> int:
    parser = PatternParser(text, meter if meter is not None else Meter())
    weight = parser.parse()
    if weight > PATTERN_SIZE_CAP:
        raise PatternError(Reason.PATTERN_SIZE)
    return weight
