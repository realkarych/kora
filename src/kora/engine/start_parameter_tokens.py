from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from kora.cel_eval.errors import raise_safe
from kora.engine.start_records import PreparedManualStartRequest


class ParameterTokenKind(StrEnum):
    OBJECT_START = "object_start"
    OBJECT_END = "object_end"
    ARRAY_START = "array_start"
    ARRAY_END = "array_end"
    COLON = "colon"
    COMMA = "comma"
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    TRUE = "true"
    FALSE = "false"
    NULL = "null"


class ParameterTokenFailure(StrEnum):
    INPUT = "invalid parameter token input"
    BUDGET = "parameter token inspection budget exceeded"
    CLOSED = "parameter token cursor is closed"


class ParameterTokenRejected(ValueError):
    def __init__(self, reason: ParameterTokenFailure) -> None:
        self.reason: ParameterTokenFailure = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class ParameterToken:
    kind: ParameterTokenKind
    start: int
    end: int


_DELIMITERS: dict[int, ParameterTokenKind] = {
    ord("{"): ParameterTokenKind.OBJECT_START,
    ord("}"): ParameterTokenKind.OBJECT_END,
    ord("["): ParameterTokenKind.ARRAY_START,
    ord("]"): ParameterTokenKind.ARRAY_END,
    ord(":"): ParameterTokenKind.COLON,
    ord(","): ParameterTokenKind.COMMA,
}
_LITERALS: dict[int, tuple[bytes, ParameterTokenKind]] = {
    ord("t"): (b"true", ParameterTokenKind.TRUE),
    ord("f"): (b"false", ParameterTokenKind.FALSE),
    ord("n"): (b"null", ParameterTokenKind.NULL),
}
_HEX_DIGITS: frozenset[int] = frozenset(b"0123456789abcdefABCDEF")
_ESCAPES: frozenset[int] = frozenset(b'"\\/bfnrt')


class ManualParameterTokenCursor:
    __slots__ = ("_source", "_remaining", "_position", "_lookahead", "_complete")
    _source: bytes | None
    _remaining: int
    _position: int
    _lookahead: int | None
    _complete: bool

    def __new__(cls, request: PreparedManualStartRequest, max_inspections: int) -> Self:
        if type(request) is not PreparedManualStartRequest:
            del cls, request, max_inspections
            raise_safe(ParameterTokenRejected(ParameterTokenFailure.INPUT))
        if type(max_inspections) is not int or max_inspections <= 0:
            del cls, request, max_inspections
            raise_safe(ParameterTokenRejected(ParameterTokenFailure.BUDGET))
        source = request.parameter_bytes()
        cursor = super().__new__(cls)
        cursor._source = source
        cursor._remaining = min(max_inspections, len(source))
        cursor._position = 0
        cursor._lookahead = None
        cursor._complete = False
        return cursor

    def close(self) -> None:
        self._source = None
        self._remaining = 0
        self._lookahead = None

    def __getstate__(self) -> None:
        del self
        raise_safe(TypeError("parameter token cursor requires explicit access"))

    def _peek(self) -> int | None:
        if self._lookahead is not None:
            return self._lookahead
        source = self._source
        if source is None or self._position == len(source):
            return None
        if self._remaining == 0:
            raise ParameterTokenRejected(ParameterTokenFailure.BUDGET)
        self._remaining -= 1
        self._lookahead = source[self._position]
        return self._lookahead

    def _advance(self) -> None:
        self._position += 1
        self._lookahead = None

    def _scan_string(self) -> None:
        self._advance()
        while True:
            byte = self._peek()
            if byte is None or byte < 32:
                raise ParameterTokenRejected(ParameterTokenFailure.INPUT)
            self._advance()
            if byte == ord('"'):
                return
            if byte != ord("\\"):
                continue
            escape = self._peek()
            if escape is None:
                raise ParameterTokenRejected(ParameterTokenFailure.INPUT)
            self._advance()
            if escape == ord("u"):
                for _ in range(4):
                    digit = self._peek()
                    if digit is None or digit not in _HEX_DIGITS:
                        raise ParameterTokenRejected(ParameterTokenFailure.INPUT)
                    self._advance()
            elif escape not in _ESCAPES:
                raise ParameterTokenRejected(ParameterTokenFailure.INPUT)

    def _scan_digits(self) -> None:
        start = self._position
        while (byte := self._peek()) is not None and ord("0") <= byte <= ord("9"):
            self._advance()
        if self._position == start:
            raise ParameterTokenRejected(ParameterTokenFailure.INPUT)

    def _scan_number(self) -> ParameterTokenKind:
        if self._peek() == ord("-"):
            self._advance()
        if self._peek() == ord("0"):
            self._advance()
        else:
            self._scan_digits()
        kind = ParameterTokenKind.INTEGER
        if self._peek() == ord("."):
            kind = ParameterTokenKind.FLOAT
            self._advance()
            self._scan_digits()
        if self._peek() in (ord("e"), ord("E")):
            kind = ParameterTokenKind.FLOAT
            self._advance()
            if self._peek() in (ord("+"), ord("-")):
                self._advance()
            self._scan_digits()
        return kind

    def _scan_kind(self, byte: int) -> ParameterTokenKind:
        delimiter = _DELIMITERS.get(byte)
        if delimiter is not None:
            self._advance()
            return delimiter
        if byte == ord('"'):
            self._scan_string()
            return ParameterTokenKind.STRING
        if byte == ord("-") or ord("0") <= byte <= ord("9"):
            return self._scan_number()
        literal = _LITERALS.get(byte)
        if literal is None:
            raise ParameterTokenRejected(ParameterTokenFailure.INPUT)
        spelling, kind = literal
        for expected in spelling:
            if self._peek() != expected:
                raise ParameterTokenRejected(ParameterTokenFailure.INPUT)
            self._advance()
        return kind

    def _next_token(self) -> ParameterToken | None:
        if self._complete:
            return None
        if self._source is None:
            raise ParameterTokenRejected(ParameterTokenFailure.CLOSED)
        start = self._position
        byte = self._peek()
        if byte is None:
            self._complete = True
            self.close()
            return None
        kind = self._scan_kind(byte)
        return ParameterToken(kind, start, self._position)

    def next_token(self) -> ParameterToken | None:
        try:
            return self._next_token()
        except ParameterTokenRejected as rejected:
            reason = rejected.reason
        self.close()
        del self
        raise_safe(ParameterTokenRejected(reason))
