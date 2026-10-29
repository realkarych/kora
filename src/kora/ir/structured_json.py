import json
import math
import re
from dataclasses import dataclass
from typing import Never, cast

from kora.ir.diagnostics import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.ir.vcs_ref_json import lexical_depth_valid

MAX_JSON_BYTES: int = 1_048_576
MAX_JSON_DEPTH: int = 16
MAX_INTEGER_BITS: int = 14_000
MAX_INTEGER_DIGITS: int = 4215
_SURROGATES: re.Pattern[str] = re.compile(r"[\ud800-\udfff]")
_ESCAPES: re.Pattern[str] = re.compile(r'["\\\x00-\x1f]')
_SHORT_ESCAPES: frozenset[str] = frozenset('"\\\b\f\n\r\t')
_MESSAGES: dict[str, str] = {
    "SO001": "StructuredOutput raw JSON exceeds its byte limit.",
    "SO002": "StructuredOutput JSON encoding is invalid.",
    "SO003": "StructuredOutput value is outside its JSON domain.",
    "SO004": "StructuredOutput canonical JSON exceeds its byte limit.",
    "SO005": "StructuredOutput revision descriptor is unsupported.",
}


class StructuredOutputValidationError(Exception):
    violation: ContractViolation

    def __init__(self, code: str) -> None:
        message = _MESSAGES[code]
        super().__init__(message)
        self.violation = ContractViolation(code=code, message=message, path=())


def refuse(code: str) -> Never:
    raise StructuredOutputValidationError(code)


@dataclass
class CanonicalBudget:
    remaining: int = MAX_JSON_BYTES

    def spend(self, count: int) -> None:
        if count > self.remaining:
            refuse("SO004")
        self.remaining -= count


def spend_string(text: str, budget: CanonicalBudget, domain_code: str) -> None:
    budget.spend(len(text) + 2)
    if _SURROGATES.search(text) is not None:
        refuse(domain_code)
    if not text.isascii():
        for offset in range(0, len(text), 4096):
            chunk = text[offset : offset + 4096]
            budget.spend(len(chunk.encode("utf-8")) - len(chunk))
    for match in _ESCAPES.finditer(text):
        budget.spend(1 if match[0] in _SHORT_ESCAPES else 5)


def admit_value(
    value: object,
    budget: CanonicalBudget,
    ancestors: set[int],
    depth: int,
    domain_code: str,
) -> JsonValue:
    kind = type(value)
    identity = id(value)
    if value is None:
        budget.spend(4)
        return None
    if type(value) is bool:
        budget.spend(4 if value else 5)
        return value
    if type(value) is int:
        if value.bit_length() > MAX_INTEGER_BITS:
            refuse(domain_code)
        budget.spend(len(str(value)))
        return value
    if type(value) is float:
        if not math.isfinite(value):
            refuse(domain_code)
        budget.spend(len(repr(value)))
        return value
    if type(value) is str:
        spend_string(value, budget, domain_code)
        return value
    if kind is not list and kind is not dict:
        refuse(domain_code)
    if depth >= MAX_JSON_DEPTH or identity in ancestors:
        refuse(domain_code)
    if kind is list:
        entries = cast("list[object]", value)
        minimum = 2 + max(0, len(entries) - 1)
        if minimum + len(entries) > budget.remaining:
            refuse("SO004")
        budget.spend(minimum)
        ancestors.add(identity)
        items: list[JsonValue] = []
        try:
            for item in entries:
                items.append(
                    admit_value(item, budget, ancestors, depth + 1, domain_code)
                )
        finally:
            ancestors.remove(identity)
        return items
    mapping = cast("dict[object, object]", value)
    minimum = 2 + max(0, len(mapping) - 1) + 3 * len(mapping)
    if minimum + len(mapping) > budget.remaining:
        refuse("SO004")
    budget.spend(2 + max(0, len(mapping) - 1))
    ancestors.add(identity)
    document: JsonObject = {}
    try:
        for key, item in mapping.items():
            if type(key) is not str:
                refuse(domain_code)
            spend_string(key, budget, domain_code)
            budget.spend(1)
            document[key] = admit_value(item, budget, ancestors, depth + 1, domain_code)
    finally:
        ancestors.remove(identity)
    return document


def snapshot(value: object, domain_code: str = "SO003") -> JsonValue:
    return admit_value(value, CanonicalBudget(), set(), 0, domain_code)


def encode_snapshot(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def parse_integer(token: str) -> int:
    digits = len(token) - int(token.startswith("-"))
    if digits > MAX_INTEGER_DIGITS:
        raise ValueError("invalid JSON integer")
    value = int(token)
    if value.bit_length() > MAX_INTEGER_BITS:
        raise ValueError("invalid JSON integer")
    return value


def parse_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("invalid JSON number")
    return value


def refuse_constant(token: str) -> Never:
    raise ValueError("invalid JSON constant")


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for name, value in pairs:
        if name in document:
            raise ValueError("duplicate JSON member")
        document[name] = value
    return document


def valid_unicode(value: JsonValue) -> bool:
    if type(value) is str:
        return _SURROGATES.search(value) is None
    if type(value) is list:
        return all(valid_unicode(item) for item in value)
    if type(value) is dict:
        return all(
            _SURROGATES.search(key) is None and valid_unicode(item)
            for key, item in value.items()
        )
    return True


def decode_json(payload: object) -> JsonValue:
    if type(payload) is not bytes:
        refuse("SO002")
    if len(payload) > MAX_JSON_BYTES:
        refuse("SO001")
    if not lexical_depth_valid(payload):
        refuse("SO002")
    valid = False
    decoded: JsonValue = None
    try:
        decoded = cast(
            "JsonValue",
            json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_int=parse_integer,
                parse_float=parse_float,
                parse_constant=refuse_constant,
            ),
        )
        valid = valid_unicode(decoded)
    except (ValueError, RecursionError, OverflowError):
        valid = False
    if not valid:
        refuse("SO002")
    return decoded
