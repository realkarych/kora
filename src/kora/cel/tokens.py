from dataclasses import dataclass
from enum import Enum
from typing import Final

from kora.cel.characters import (
    COMMENT_END,
    DIGITS,
    HEX_DIGITS,
    IDENTIFIER_PART,
    IDENTIFIER_START,
    NEWLINES,
    OCTAL_DIGITS,
    WHITESPACE,
    is_rejected,
)
from kora.cel.diagnostics import Reason, fail
from kora.cel.limits import MAX_CODE_POINT
from kora.cel.meter import Meter


class TokenKind(Enum):
    INT = "int"
    UINT = "uint"
    DOUBLE = "double"
    STRING = "string"
    BYTES = "bytes"
    TRUE = "true"
    FALSE = "false"
    NULL = "null"
    IDENTIFIER = "identifier"
    IN = "in"
    DOT = "."
    COMMA = ","
    COLON = ":"
    QUESTION = "?"
    LEFT_PAREN = "("
    RIGHT_PAREN = ")"
    LEFT_BRACKET = "["
    RIGHT_BRACKET = "]"
    LEFT_BRACE = "{"
    RIGHT_BRACE = "}"
    PLUS = "+"
    MINUS = "-"
    STAR = "*"
    SLASH = "/"
    PERCENT = "%"
    BANG = "!"
    AND = "&&"
    OR = "||"
    EQUAL = "=="
    NOT_EQUAL = "!="
    LESS = "<"
    LESS_EQUAL = "<="
    GREATER = ">"
    GREATER_EQUAL = ">="
    END = "end"


TokenValue = int | float | str | bytes | None


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    offset: int
    value: TokenValue = None


KEYWORDS: Final[dict[str, TokenKind]] = {
    "true": TokenKind.TRUE,
    "false": TokenKind.FALSE,
    "null": TokenKind.NULL,
    "in": TokenKind.IN,
}

TWO_CHARACTER: Final[dict[str, TokenKind]] = {
    "&&": TokenKind.AND,
    "||": TokenKind.OR,
    "==": TokenKind.EQUAL,
    "!=": TokenKind.NOT_EQUAL,
    "<=": TokenKind.LESS_EQUAL,
    ">=": TokenKind.GREATER_EQUAL,
}

ONE_CHARACTER: Final[dict[str, TokenKind]] = {
    ".": TokenKind.DOT,
    ",": TokenKind.COMMA,
    ":": TokenKind.COLON,
    "?": TokenKind.QUESTION,
    "(": TokenKind.LEFT_PAREN,
    ")": TokenKind.RIGHT_PAREN,
    "[": TokenKind.LEFT_BRACKET,
    "]": TokenKind.RIGHT_BRACKET,
    "{": TokenKind.LEFT_BRACE,
    "}": TokenKind.RIGHT_BRACE,
    "+": TokenKind.PLUS,
    "-": TokenKind.MINUS,
    "*": TokenKind.STAR,
    "/": TokenKind.SLASH,
    "%": TokenKind.PERCENT,
    "!": TokenKind.BANG,
    "<": TokenKind.LESS,
    ">": TokenKind.GREATER,
}

SIMPLE_ESCAPES: Final[dict[str, int]] = {
    "a": 0x07,
    "b": 0x08,
    "f": 0x0C,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "v": 0x0B,
    "\\": 0x5C,
    "?": 0x3F,
    '"': 0x22,
    "'": 0x27,
    "`": 0x60,
}


class Lexer:
    def __init__(self, text: str, meter: Meter) -> None:
        self.text: str = text
        self.position: int = 0
        self.tokens: list[Token] = []
        self.meter: Meter = meter

    def peek(self, ahead: int = 0) -> str:
        index = self.position + ahead
        return self.text[index] if index < len(self.text) else ""

    def advance(self) -> str:
        character = self.text[self.position]
        if is_rejected(character):
            raise fail(Reason.INVALID_CHARACTER, self.position)
        self.position += 1
        self.meter.tick()
        return character

    def run(self) -> list[Token]:
        while True:
            self.skip_blank()
            if self.position >= len(self.text):
                self.tokens.append(Token(TokenKind.END, self.position))
                return self.tokens
            self.tokens.append(self.next_token())

    def skip_blank(self) -> None:
        while self.position < len(self.text):
            character = self.peek()
            if character in WHITESPACE:
                self.advance()
            elif character == "/" and self.peek(1) == "/":
                self.comment()
            else:
                return

    def comment(self) -> None:
        while self.position < len(self.text) and self.peek() != COMMENT_END:
            if self.peek() == "\r" and self.peek(1) != COMMENT_END:
                raise fail(Reason.INVALID_CHARACTER, self.position)
            self.advance()

    def next_token(self) -> Token:
        start = self.position
        character = self.peek()
        if is_rejected(character):
            raise fail(Reason.INVALID_CHARACTER, start)
        if character == "`":
            raise fail(Reason.QUOTED_FIELD, start)
        prefix = self.literal_prefix()
        if prefix is not None:
            return self.string_literal(start, prefix)
        if character in IDENTIFIER_START:
            return self.word(start)
        if character in DIGITS or (character == "." and self.peek(1) in DIGITS):
            return self.number(start)
        pair = self.text[start : start + 2]
        if pair in TWO_CHARACTER:
            self.advance()
            self.advance()
            return Token(TWO_CHARACTER[pair], start)
        if character in ONE_CHARACTER:
            self.advance()
            return Token(ONE_CHARACTER[character], start)
        raise fail(Reason.INVALID_CHARACTER, start)

    def literal_prefix(self) -> str | None:
        for prefix in ("", "r", "b", "br"):
            if self.text[self.position : self.position + len(prefix)].lower() != prefix:
                continue
            if self.peek(len(prefix)) in ("'", '"'):
                return prefix
        return None

    def word(self, start: int) -> Token:
        while self.position < len(self.text) and self.peek() in IDENTIFIER_PART:
            self.advance()
        name = self.text[start : self.position]
        kind = KEYWORDS.get(name)
        if kind is not None:
            return Token(kind, start)
        return Token(TokenKind.IDENTIFIER, start, name)

    def digits(self, allowed: frozenset[str]) -> str:
        begin = self.position
        while self.position < len(self.text) and self.peek() in allowed:
            self.advance()
        return self.text[begin : self.position]

    def number(self, start: int) -> Token:
        if self.peek() == "0" and self.peek(1) == "x":
            self.advance()
            self.advance()
            hexadecimal = self.digits(HEX_DIGITS)
            if not hexadecimal:
                raise fail(Reason.INVALID_LITERAL, start)
            return self.integer_token(start, int(hexadecimal, 16))
        whole = self.digits(DIGITS)
        is_double = False
        if self.peek() == "." and self.peek(1) in DIGITS:
            is_double = True
            self.advance()
            self.digits(DIGITS)
        if self.peek() in ("e", "E"):
            sign = 1 if self.peek(1) in ("+", "-") else 0
            if self.peek(1 + sign) in DIGITS:
                is_double = True
                for _ in range(1 + sign):
                    self.advance()
                self.digits(DIGITS)
        if is_double:
            return self.double_token(start)
        return self.integer_token(start, int(whole))

    def integer_token(self, start: int, value: int) -> Token:
        if self.peek() in ("u", "U"):
            self.advance()
            return Token(TokenKind.UINT, start, value)
        return Token(TokenKind.INT, start, value)

    def double_token(self, start: int) -> Token:
        value = float(self.text[start : self.position])
        if value in (float("inf"), float("-inf")):
            raise fail(Reason.INVALID_LITERAL, start)
        return Token(TokenKind.DOUBLE, start, value)

    def string_literal(self, start: int, prefix: str) -> Token:
        for _ in prefix:
            self.advance()
        raw = "r" in prefix
        is_bytes = "b" in prefix
        quote = self.peek()
        delimiter = (
            quote * 3
            if self.text[self.position : self.position + 3] == quote * 3
            else quote
        )
        for _ in delimiter:
            self.advance()
        points: list[int] = []
        while True:
            if self.position >= len(self.text):
                raise fail(Reason.UNTERMINATED_LITERAL, start)
            if self.text[self.position : self.position + len(delimiter)] == delimiter:
                for _ in delimiter:
                    self.advance()
                break
            character = self.peek()
            if len(delimiter) == 1 and character in NEWLINES:
                raise fail(Reason.UNTERMINATED_LITERAL, start)
            if character == "\\" and not raw:
                points.extend(self.escape(is_bytes))
            else:
                self.advance()
                points.extend(self.encoded(ord(character), is_bytes))
        if is_bytes:
            return Token(TokenKind.BYTES, start, bytes(points))
        return Token(TokenKind.STRING, start, "".join(map(chr, points)))

    def encoded(self, point: int, is_bytes: bool) -> list[int]:
        if is_bytes:
            return list(chr(point).encode("utf-8"))
        return [point]

    def escape(self, is_bytes: bool) -> list[int]:
        start = self.position
        self.advance()
        if self.position >= len(self.text):
            raise fail(Reason.INVALID_ESCAPE, start)
        selector = self.advance()
        if selector in SIMPLE_ESCAPES:
            return [SIMPLE_ESCAPES[selector]]
        if selector in ("x", "X"):
            return [self.fixed_digits(start, 2, HEX_DIGITS, 16)]
        if selector in ("u", "U") and is_bytes:
            raise fail(Reason.INVALID_ESCAPE, start)
        if selector == "u":
            return self.code_point(
                start, self.fixed_digits(start, 4, HEX_DIGITS, 16), is_bytes
            )
        if selector == "U":
            return self.code_point(
                start, self.fixed_digits(start, 8, HEX_DIGITS, 16), is_bytes
            )
        if selector in "0123":
            rest = self.fixed_digits(start, 2, OCTAL_DIGITS, 8)
            return [int(selector, 8) * 64 + rest]
        raise fail(Reason.INVALID_ESCAPE, start)

    def fixed_digits(
        self, start: int, count: int, allowed: frozenset[str], base: int
    ) -> int:
        text = self.text[self.position : self.position + count]
        if len(text) != count or any(character not in allowed for character in text):
            raise fail(Reason.INVALID_ESCAPE, start)
        for _ in range(count):
            self.advance()
        return int(text, base)

    def code_point(self, start: int, point: int, is_bytes: bool) -> list[int]:
        if point > MAX_CODE_POINT or 0xD800 <= point <= 0xDFFF:
            raise fail(Reason.INVALID_ESCAPE, start)
        return self.encoded(point, is_bytes)


def lex(text: str, meter: Meter) -> list[Token]:
    return Lexer(text, meter).run()
