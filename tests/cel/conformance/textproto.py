from dataclasses import dataclass
from typing import Final

DIGITS: Final[frozenset[str]] = frozenset("0123456789")
HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdefABCDEF")
OCTAL_DIGITS: Final[frozenset[str]] = frozenset("01234567")
NAME_START: Final[frozenset[str]] = frozenset(
    "_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)
NAME_PART: Final[frozenset[str]] = NAME_START | DIGITS
EXTENSION_PART: Final[frozenset[str]] = NAME_PART | frozenset("./")
NUMBER_PART: Final[frozenset[str]] = HEX_DIGITS | frozenset("xX.+-")
WHITESPACE: Final[frozenset[str]] = frozenset(" \t\r\n")
SIMPLE_ESCAPES: Final[dict[str, int]] = {
    "a": 0x07,
    "b": 0x08,
    "f": 0x0C,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "v": 0x0B,
    "\\": 0x5C,
    "'": 0x27,
    '"': 0x22,
    "?": 0x3F,
}


class TextprotoError(Exception):
    def __init__(self, reason: str, offset: int) -> None:
        super().__init__(f"{reason} at offset {offset}")
        self.reason: str = reason
        self.offset: int = offset


@dataclass(frozen=True)
class Word:
    text: str


@dataclass(frozen=True)
class Number:
    text: str


@dataclass(frozen=True)
class Text:
    data: bytes


@dataclass(frozen=True)
class Message:
    fields: tuple["FieldEntry", ...]


Scalar = Word | Number | Text
FieldValue = Scalar | Message


@dataclass(frozen=True)
class FieldEntry:
    name: str
    value: FieldValue


class Reader:
    def __init__(self, text: str) -> None:
        self.text: str = text
        self.position: int = 0

    def error(self, reason: str) -> TextprotoError:
        return TextprotoError(reason, self.position)

    def peek(self) -> str:
        return self.text[self.position] if self.position < len(self.text) else ""

    def skip_blank(self) -> None:
        while self.position < len(self.text):
            character = self.text[self.position]
            if character in WHITESPACE:
                self.position += 1
            elif character == "#":
                end = self.text.find("\n", self.position)
                self.position = len(self.text) if end < 0 else end + 1
            else:
                return

    def expect(self, character: str) -> None:
        self.skip_blank()
        if self.peek() != character:
            raise self.error(f"expected {character!r}")
        self.position += 1

    def read_file(self) -> Message:
        message = self.read_fields(closing="")
        self.skip_blank()
        if self.position != len(self.text):
            raise self.error("unexpected text after the last field")
        return message

    def read_fields(self, closing: str) -> Message:
        entries: list[FieldEntry] = []
        while True:
            self.skip_blank()
            if self.peek() == closing:
                return Message(tuple(entries))
            entries.extend(self.read_field())
            self.skip_blank()
            if self.peek() in (",", ";"):
                self.position += 1

    def read_name(self) -> str:
        self.skip_blank()
        if self.peek() == "[":
            self.position += 1
            start = self.position
            while self.peek() in EXTENSION_PART and self.peek() != "":
                self.position += 1
            name = self.text[start : self.position]
            self.expect("]")
            return f"[{name}]"
        return self.read_word()

    def read_word(self) -> str:
        start = self.position
        if self.peek() not in NAME_START or self.peek() == "":
            raise self.error("expected a field name")
        while self.peek() in NAME_PART and self.peek() != "":
            self.position += 1
        return self.text[start : self.position]

    def read_field(self) -> list[FieldEntry]:
        name = self.read_name()
        self.skip_blank()
        has_colon = self.peek() == ":"
        if has_colon:
            self.position += 1
            self.skip_blank()
        if self.peek() in ("{", "<"):
            return [FieldEntry(name, self.read_message())]
        if not has_colon:
            raise self.error("expected ':' before a scalar value")
        if self.peek() == "[":
            return [FieldEntry(name, value) for value in self.read_list()]
        return [FieldEntry(name, self.read_scalar())]

    def read_message(self) -> Message:
        opening = self.peek()
        closing = "}" if opening == "{" else ">"
        self.position += 1
        message = self.read_fields(closing)
        self.expect(closing)
        return message

    def read_list(self) -> list[FieldValue]:
        self.position += 1
        values: list[FieldValue] = []
        self.skip_blank()
        while self.peek() != "]":
            if self.peek() in ("{", "<"):
                values.append(self.read_message())
            else:
                values.append(self.read_scalar())
            self.skip_blank()
            if self.peek() == ",":
                self.position += 1
                self.skip_blank()
            elif self.peek() != "]":
                raise self.error("expected ',' or ']'")
        self.position += 1
        return values

    def read_scalar(self) -> Scalar:
        self.skip_blank()
        character = self.peek()
        if character in ("'", '"'):
            data = bytearray()
            while self.peek() in ("'", '"'):
                data.extend(self.read_string())
                self.skip_blank()
            return Text(bytes(data))
        if character in DIGITS or character in ("-", "."):
            return self.read_number()
        return Word(self.read_word())

    def read_number(self) -> Number:
        start = self.position
        if self.peek() == "-":
            self.position += 1
        while self.peek() in NUMBER_PART and self.peek() != "":
            self.position += 1
        if self.text[start : self.position] in ("", "-"):
            if self.peek() in NAME_START:
                return Number(self.text[start : self.position] + self.read_word())
            raise self.error("expected a number")
        return Number(self.text[start : self.position])

    def read_string(self) -> bytes:
        quote = self.peek()
        self.position += 1
        data = bytearray()
        while True:
            character = self.peek()
            if character == "":
                raise self.error("unterminated string")
            if character == "\n":
                raise self.error("newline in string")
            self.position += 1
            if character == quote:
                return bytes(data)
            if character == "\\":
                data.extend(self.read_escape())
            else:
                data.extend(character.encode("utf-8"))

    def read_digits(self, allowed: frozenset[str], limit: int) -> str:
        start = self.position
        while (
            self.position - start < limit
            and self.peek() in allowed
            and self.peek() != ""
        ):
            self.position += 1
        return self.text[start : self.position]

    def read_escape(self) -> bytes:
        selector = self.peek()
        if selector == "":
            raise self.error("unterminated escape")
        self.position += 1
        if selector in SIMPLE_ESCAPES:
            return bytes([SIMPLE_ESCAPES[selector]])
        if selector in ("x", "X"):
            digits = self.read_digits(HEX_DIGITS, 2)
            if not digits:
                raise self.error("expected hexadecimal digits")
            return bytes([int(digits, 16)])
        if selector in OCTAL_DIGITS:
            self.position -= 1
            digits = self.read_digits(OCTAL_DIGITS, 3)
            value = int(digits, 8)
            if value > 0xFF:
                raise self.error("octal escape out of range")
            return bytes([value])
        if selector in ("u", "U"):
            width = 4 if selector == "u" else 8
            digits = self.read_digits(HEX_DIGITS, width)
            if len(digits) != width:
                raise self.error("expected a full unicode escape")
            point = int(digits, 16)
            if point > 0x10FFFF or 0xD800 <= point <= 0xDFFF:
                raise self.error("unicode escape out of range")
            return chr(point).encode("utf-8")
        raise self.error(f"unknown escape {selector!r}")


def read_textproto(text: str) -> Message:
    return Reader(text).read_file()
