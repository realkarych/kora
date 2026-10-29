import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tests.cel.conformance.textproto import (
    FieldEntry,
    FieldValue,
    Message,
    Number,
    Text,
    Word,
    read_textproto,
)

TESTDATA: Final[Path] = Path(__file__).resolve().parent / "testdata"
FLOAT_WORDS: Final[dict[str, float]] = {
    "inf": math.inf,
    "Infinity": math.inf,
    "-inf": -math.inf,
    "-Infinity": -math.inf,
    "nan": math.nan,
}
INTEGER_TEXT: Final[re.Pattern[str]] = re.compile(r"-?(0|[1-9][0-9]*|0x[0-9a-fA-F]+)")
FLOAT_TEXT: Final[re.Pattern[str]] = re.compile(
    r"-?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?"
)
INT64_RANGE: Final[range] = range(-(2**63), 2**63)
UINT64_RANGE: Final[range] = range(0, 2**64)
NANOS_RANGE: Final[range] = range(-999_999_999, 1_000_000_000)
DURATION_TYPE_URL: Final[str] = "type.googleapis.com/google.protobuf.Duration"
PRIMITIVES: Final[frozenset[str]] = frozenset(
    {"BOOL", "INT64", "UINT64", "DOUBLE", "STRING", "BYTES"}
)


class CaseModelError(Exception):
    pass


@dataclass(frozen=True)
class NullValue:
    pass


@dataclass(frozen=True)
class BoolValue:
    value: bool


@dataclass(frozen=True)
class IntValue:
    value: int


@dataclass(frozen=True)
class UintValue:
    value: int


@dataclass(frozen=True)
class DoubleValue:
    value: float


@dataclass(frozen=True)
class StringValue:
    value: str


@dataclass(frozen=True)
class BytesValue:
    value: bytes


@dataclass(frozen=True)
class TypeValue:
    name: str


@dataclass(frozen=True)
class ListValue:
    values: tuple["Value", ...]


@dataclass(frozen=True)
class MapValue:
    entries: tuple[tuple["Value", "Value"], ...]


@dataclass(frozen=True)
class ObjectValue:
    type_url: str


@dataclass(frozen=True)
class DurationValue:
    seconds: int
    nanos: int


Value = (
    NullValue
    | BoolValue
    | IntValue
    | UintValue
    | DoubleValue
    | StringValue
    | BytesValue
    | TypeValue
    | ListValue
    | MapValue
    | ObjectValue
    | DurationValue
)


@dataclass(frozen=True)
class PrimitiveType:
    name: str


@dataclass(frozen=True)
class ListType:
    element: "DeclaredType"


@dataclass(frozen=True)
class MapType:
    key: "DeclaredType"
    value: "DeclaredType"


@dataclass(frozen=True)
class MessageType:
    name: str


@dataclass(frozen=True)
class NullType:
    pass


DeclaredType = PrimitiveType | ListType | MapType | MessageType | NullType


@dataclass(frozen=True)
class Declaration:
    name: str
    type: DeclaredType


@dataclass(frozen=True)
class ExpectedValue:
    value: Value


@dataclass(frozen=True)
class ExpectedError:
    messages: tuple[str, ...]


Expected = ExpectedValue | ExpectedError


@dataclass(frozen=True)
class Case:
    file: str
    section: str
    name: str
    expression: str
    disable_check: bool
    container: str
    declarations: tuple[Declaration, ...]
    bindings: tuple[tuple[str, Value], ...]
    expected: Expected


@dataclass(frozen=True)
class CaseFile:
    name: str
    cases: tuple[Case, ...]


class Fields:
    def __init__(self, message: Message, allowed: frozenset[str], context: str) -> None:
        self.entries: tuple[FieldEntry, ...] = message.fields
        self.context: str = context
        unknown = {entry.name for entry in message.fields} - allowed
        if unknown:
            raise CaseModelError(f"{context}: unknown fields {sorted(unknown)}")

    def repeated(self, name: str) -> list[FieldValue]:
        return [entry.value for entry in self.entries if entry.name == name]

    def optional(self, name: str) -> FieldValue | None:
        values = self.repeated(name)
        if len(values) > 1:
            raise CaseModelError(f"{self.context}: field {name} repeats")
        return values[0] if values else None

    def required(self, name: str) -> FieldValue:
        value = self.optional(name)
        if value is None:
            raise CaseModelError(f"{self.context}: field {name} is missing")
        return value

    def text(self, name: str, default: str = "") -> str:
        value = self.optional(name)
        if value is None:
            return default
        return as_text(value, self.context)

    def flag(self, name: str) -> bool:
        value = self.optional(name)
        if value is None:
            return False
        if isinstance(value, Word) and value.text in ("true", "false"):
            return value.text == "true"
        raise CaseModelError(f"{self.context}: field {name} is not a boolean")


def as_text(value: FieldValue, context: str) -> str:
    if not isinstance(value, Text):
        raise CaseModelError(f"{context}: expected a string")
    try:
        return value.data.decode("utf-8")
    except UnicodeDecodeError:
        raise CaseModelError(f"{context}: a string is not UTF-8") from None


def as_message(value: FieldValue, context: str) -> Message:
    if not isinstance(value, Message):
        raise CaseModelError(f"{context}: expected a message")
    return value


def as_integer(value: FieldValue, context: str, allowed: range) -> int:
    if not isinstance(value, Number) or not INTEGER_TEXT.fullmatch(value.text):
        raise CaseModelError(f"{context}: expected a decimal or hexadecimal integer")
    number = int(value.text, 0)
    if number not in allowed:
        raise CaseModelError(f"{context}: integer out of range")
    return number


def as_float(value: FieldValue, context: str) -> float:
    if isinstance(value, Word | Number) and value.text in FLOAT_WORDS:
        return FLOAT_WORDS[value.text]
    if not isinstance(value, Number) or not FLOAT_TEXT.fullmatch(value.text):
        raise CaseModelError(f"{context}: expected a number")
    return float(value.text)


VALUE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "null_value",
        "bool_value",
        "int64_value",
        "uint64_value",
        "double_value",
        "string_value",
        "bytes_value",
        "type_value",
        "list_value",
        "map_value",
        "object_value",
    }
)


def read_value(message: Message, context: str) -> Value:
    fields = Fields(message, VALUE_FIELDS, context)
    if len(fields.entries) != 1:
        raise CaseModelError(f"{context}: a value has exactly one kind")
    entry = fields.entries[0]
    name = entry.name
    raw = entry.value
    if name == "null_value":
        if isinstance(raw, Word) and raw.text == "NULL_VALUE":
            return NullValue()
        if isinstance(raw, Number) and as_integer(raw, context, range(0, 1)) == 0:
            return NullValue()
        raise CaseModelError(f"{context}: invalid null value")
    if name == "bool_value":
        return BoolValue(Fields(Message((entry,)), VALUE_FIELDS, context).flag(name))
    if name == "int64_value":
        return IntValue(as_integer(raw, context, INT64_RANGE))
    if name == "uint64_value":
        return UintValue(as_integer(raw, context, UINT64_RANGE))
    if name == "double_value":
        return DoubleValue(as_float(raw, context))
    if name == "string_value":
        return StringValue(as_text(raw, context))
    if name == "bytes_value":
        if not isinstance(raw, Text):
            raise CaseModelError(f"{context}: expected bytes")
        return BytesValue(raw.data)
    if name == "type_value":
        return TypeValue(as_text(raw, context))
    if name == "list_value":
        items = Fields(as_message(raw, context), frozenset({"values"}), context)
        return ListValue(
            tuple(
                read_value(as_message(item, context), context)
                for item in items.repeated("values")
            )
        )
    if name == "map_value":
        entries = Fields(as_message(raw, context), frozenset({"entries"}), context)
        pairs: list[tuple[Value, Value]] = []
        for item in entries.repeated("entries"):
            pair = Fields(
                as_message(item, context), frozenset({"key", "value"}), context
            )
            pairs.append(
                (
                    read_value(as_message(pair.required("key"), context), context),
                    read_value(as_message(pair.required("value"), context), context),
                )
            )
        return MapValue(tuple(pairs))
    body = as_message(raw, context)
    if len(body.fields) != 1 or not body.fields[0].name.startswith("["):
        raise CaseModelError(f"{context}: an object value names its type")
    type_url = body.fields[0].name.strip("[]")
    if not type_url:
        raise CaseModelError(f"{context}: an object value names its type")
    if type_url == DURATION_TYPE_URL:
        return read_duration(as_message(body.fields[0].value, context), context)
    return ObjectValue(type_url)


def read_duration(message: Message, context: str) -> DurationValue:
    fields = Fields(message, frozenset({"seconds", "nanos"}), context)
    seconds = fields.optional("seconds")
    nanos = fields.optional("nanos")
    return DurationValue(
        0 if seconds is None else as_integer(seconds, context, INT64_RANGE),
        0 if nanos is None else as_integer(nanos, context, NANOS_RANGE),
    )


def read_type(message: Message, context: str) -> DeclaredType:
    fields = Fields(
        message,
        frozenset({"primitive", "list_type", "map_type", "message_type", "null"}),
        context,
    )
    if len(fields.entries) != 1:
        raise CaseModelError(f"{context}: a type has exactly one kind")
    entry = fields.entries[0]
    raw = entry.value
    if entry.name == "primitive":
        if not isinstance(raw, Word) or raw.text not in PRIMITIVES:
            raise CaseModelError(f"{context}: unknown primitive type")
        return PrimitiveType(raw.text)
    if entry.name == "list_type":
        inner = Fields(as_message(raw, context), frozenset({"elem_type"}), context)
        return ListType(
            read_type(as_message(inner.required("elem_type"), context), context)
        )
    if entry.name == "map_type":
        inner = Fields(
            as_message(raw, context), frozenset({"key_type", "value_type"}), context
        )
        return MapType(
            read_type(as_message(inner.required("key_type"), context), context),
            read_type(as_message(inner.required("value_type"), context), context),
        )
    if entry.name == "message_type":
        return MessageType(as_text(raw, context))
    if not isinstance(raw, Word) or raw.text != "NULL_VALUE":
        raise CaseModelError(f"{context}: invalid null type")
    return NullType()


def read_declaration(message: Message, context: str) -> Declaration:
    fields = Fields(message, frozenset({"name", "ident"}), context)
    ident = Fields(
        as_message(fields.required("ident"), context), frozenset({"type"}), context
    )
    return Declaration(
        fields.text("name"),
        read_type(as_message(ident.required("type"), context), context),
    )


def read_binding(message: Message, context: str) -> tuple[str, Value]:
    fields = Fields(message, frozenset({"key", "value"}), context)
    holder = Fields(
        as_message(fields.required("value"), context), frozenset({"value"}), context
    )
    return fields.text("key"), read_value(
        as_message(holder.required("value"), context), context
    )


TEST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "description",
        "expr",
        "disable_check",
        "container",
        "type_env",
        "bindings",
        "value",
        "eval_error",
    }
)


def read_expected(fields: Fields, context: str) -> Expected:
    value = fields.optional("value")
    error = fields.optional("eval_error")
    if value is not None and error is not None:
        raise CaseModelError(f"{context}: two result matchers")
    if error is not None:
        errors = Fields(as_message(error, context), frozenset({"errors"}), context)
        messages: list[str] = []
        for status in errors.repeated("errors"):
            detail = Fields(
                as_message(status, context), frozenset({"message"}), context
            )
            messages.append(detail.text("message"))
        return ExpectedError(tuple(messages))
    if value is None:
        return ExpectedValue(BoolValue(True))
    return ExpectedValue(read_value(as_message(value, context), context))


def read_case(file: str, section: str, message: Message) -> Case:
    fields = Fields(message, TEST_FIELDS, f"{file}/{section}")
    name = fields.text("name")
    context = f"{file}/{section}/{name}"
    fields.text("description")
    bindings = tuple(
        read_binding(as_message(item, context), context)
        for item in fields.repeated("bindings")
    )
    if len({key for key, _ in bindings}) != len(bindings):
        raise CaseModelError(f"{context}: a binding key repeats")
    return Case(
        file=file,
        section=section,
        name=name,
        expression=fields.text("expr"),
        disable_check=fields.flag("disable_check"),
        container=fields.text("container"),
        declarations=tuple(
            read_declaration(as_message(item, context), context)
            for item in fields.repeated("type_env")
        ),
        bindings=bindings,
        expected=read_expected(fields, context),
    )


def read_cases(name: str, text: str) -> CaseFile:
    document = Fields(
        read_textproto(text), frozenset({"name", "description", "section"}), name
    )
    cases: list[Case] = []
    for section_value in document.repeated("section"):
        section = Fields(
            as_message(section_value, name),
            frozenset({"name", "description", "test"}),
            name,
        )
        section_name = section.text("name")
        section.text("description")
        cases.extend(
            read_case(name, section_name, as_message(item, name))
            for item in section.repeated("test")
        )
    document.text("description")
    return CaseFile(document.text("name"), tuple(cases))


def vendored_files() -> Iterator[Path]:
    yield from sorted(TESTDATA.glob("*.textproto"))


def vendored_cases() -> Iterator[Case]:
    for path in vendored_files():
        yield from read_cases(path.stem, path.read_text(encoding="utf-8")).cases
