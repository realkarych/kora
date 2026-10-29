import math
from json.encoder import encode_basestring
from typing import TypeAlias, cast

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic_core import CoreSchema, PydanticCustomError, core_schema

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = dict[str, JsonValue]

DEFAULT_MAX_DEPTH: int = 16
MAX_CANONICAL_BYTES: int = 1_048_576
MAX_INTEGER_BITS: int = 14_000
JSON_RULES: frozenset[str] = frozenset(
    {"json_object", "json_depth", "json_key", "json_float", "json_scalar", "json_size"}
)


class JsonShapeError(ValueError):
    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


class CanonicalBudget:
    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def spend(self, size: int) -> None:
        if size > self.remaining:
            raise JsonShapeError("json_size")
        self.remaining -= size


def parse_json_object(
    value: object,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_bytes: int = MAX_CANONICAL_BYTES,
) -> JsonObject:
    if not isinstance(value, dict):
        raise JsonShapeError("json_object")
    budget = CanonicalBudget(max_bytes)
    return parse_object(cast("dict[object, object]", value), max_depth, 1, budget)


def parse_object(
    value: dict[object, object], max_depth: int, depth: int, budget: CanonicalBudget
) -> JsonObject:
    if depth > max_depth:
        raise JsonShapeError("json_depth")
    entries = list(dict[object, object].items(value))
    budget.spend(2 + max(len(entries) - 1, 0))
    parsed: JsonObject = {}
    for key, item in entries:
        if not isinstance(key, str):
            raise JsonShapeError("json_key")
        name = key if type(key) is str else str.__str__(key)
        if name in parsed:
            raise JsonShapeError("json_key")
        spend_string(budget, name, "json_key", 1)
        parsed[name] = parse_value(item, max_depth, depth, budget)
    return parsed


def parse_value(
    value: object, max_depth: int, depth: int, budget: CanonicalBudget
) -> JsonValue:
    if type(value) is str:
        spend_string(budget, value, "json_scalar", 0)
        return value
    if value is None or isinstance(value, bool):
        budget.spend(4 if value is not False else 5)
        return value
    if isinstance(value, int):
        return parse_integer(int.__int__(value), budget)
    if isinstance(value, str):
        text = str.__str__(value)
        spend_string(budget, text, "json_scalar", 0)
        return text
    if isinstance(value, float):
        return parse_float(float.__float__(value), budget)
    if isinstance(value, list):
        if depth + 1 > max_depth:
            raise JsonShapeError("json_depth")
        items = list[object].copy(cast("list[object]", value))
        budget.spend(2 + max(len(items) - 1, 0))
        return [parse_value(item, max_depth, depth + 1, budget) for item in items]
    if isinstance(value, dict):
        nested = cast("dict[object, object]", value)
        return parse_object(nested, max_depth, depth + 1, budget)
    raise JsonShapeError("json_scalar")


def parse_integer(value: int, budget: CanonicalBudget) -> int:
    if value.bit_length() > MAX_INTEGER_BITS:
        raise JsonShapeError("json_size")
    budget.spend(len(repr(value)))
    return value


def parse_float(value: float, budget: CanonicalBudget) -> float:
    if not math.isfinite(value):
        raise JsonShapeError("json_float")
    budget.spend(len(repr(value)))
    return value


def spend_string(budget: CanonicalBudget, text: str, rule: str, suffix: int) -> None:
    size = len(text) + 2 + suffix
    if text.isascii() and text.isprintable():
        if '"' in text:
            size += text.count('"')
        if "\\" in text:
            size += text.count("\\")
        budget.spend(size)
        return
    budget.spend(size)
    budget.spend(escaped_extra(text, rule))


def escaped_extra(text: str, rule: str) -> int:
    if text.isprintable():
        return utf8_extra(text) + text.count('"') + text.count("\\")
    encoded = canonical_string(text)
    if encoded is None:
        raise JsonShapeError(rule)
    return len(encoded) - len(text) - 2


def utf8_extra(text: str) -> int:
    return len(text.encode("utf-8")) - len(text)


def utf8_length(text: str) -> int | None:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def fits_utf8(text: str, limit: int) -> bool:
    length = utf8_length(text)
    return length is not None and length <= limit


def canonical_string(text: str) -> bytes | None:
    try:
        return encode_basestring(text).encode("utf-8")
    except UnicodeEncodeError:
        return None


class JsonObjectField(dict[str, JsonValue]):
    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[object], handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_plain_validator_function(validate_json_object_field)

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> dict[str, str]:
        return {"type": "object"}


def validate_json_object_field(value: object) -> JsonObjectField:
    parsed = parsed_or_none(value)
    if parsed is None:
        raise PydanticCustomError("json_object_invalid", "the JSON object is invalid")
    return JsonObjectField(parsed)


def parsed_or_none(
    value: object, max_bytes: int = MAX_CANONICAL_BYTES
) -> JsonObject | None:
    try:
        return parse_json_object(value, max_bytes=max_bytes)
    except JsonShapeError:
        return None
