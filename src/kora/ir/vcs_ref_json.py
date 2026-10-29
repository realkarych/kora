import json
import math
import re
from enum import Enum
from typing import Never, cast

from kora.ir.json import JsonObject, JsonValue

MAX_JSON_BYTES: int = 1_048_576
MAX_JSON_DEPTH: int = 16
INVALID_SCALAR: re.Pattern[str] = re.compile(r"[\u0000\ud800-\udfff]")
_SHORT_CONTROLS: str = "\b\f\n\r\t"
_LONG_CONTROLS: tuple[str, ...] = tuple(
    chr(value) for value in range(32) if chr(value) not in _SHORT_CONTROLS
)


class JsonRefusal(Enum):
    SIZE = "size"
    DECODE = "decode"


def lexical_depth_valid(
    payload: bytes | bytearray, max_depth: int = MAX_JSON_DEPTH
) -> bool:
    depth = 0
    quoted = False
    escaped = False
    for character in payload:
        if quoted:
            if escaped:
                escaped = False
            elif character == 92:
                escaped = True
            elif character == 34:
                quoted = False
        elif character == 34:
            quoted = True
        elif character in (123, 91):
            depth += 1
            if depth > max_depth:
                return False
        elif character in (125, 93):
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not quoted


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value


def refuse_constant(value: str) -> Never:
    raise ValueError("nonfinite JSON constant")


def valid_scalars(value: object) -> bool:
    if type(value) is str:
        return INVALID_SCALAR.search(value) is None
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is dict:
        return all(
            INVALID_SCALAR.search(key) is None and valid_scalars(item)
            for key, item in cast("dict[str, object]", value).items()
        )
    if type(value) is list:
        return all(valid_scalars(item) for item in cast("list[object]", value))
    return True


def decode_strict_json(
    payload: bytes | bytearray, max_bytes: int, max_depth: int
) -> JsonValue | JsonRefusal:
    if len(payload) > max_bytes:
        return JsonRefusal.SIZE
    if not lexical_depth_valid(payload, max_depth):
        return JsonRefusal.DECODE
    try:
        value: object = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=refuse_constant,
        )
    except (ValueError, RecursionError):
        return JsonRefusal.DECODE
    if not valid_scalars(value):
        return JsonRefusal.DECODE
    return cast("JsonValue", value)


def decode_vcs_ref_json(payload: bytes) -> JsonValue | JsonRefusal:
    return decode_strict_json(payload, MAX_JSON_BYTES, MAX_JSON_DEPTH)


def canonical_size(value: JsonValue, remaining: int = MAX_JSON_BYTES) -> int:
    if isinstance(value, str):
        size = len(value) + 2
        if size > remaining:
            return remaining + 1
        encoded_size = len(value) if value.isascii() else len(value.encode("utf-8"))
        if value.isprintable():
            return encoded_size + 2 + value.count('"') + value.count("\\")
        size = (
            encoded_size
            + 2
            + value.count('"')
            + value.count("\\")
            + sum(value.count(character) for character in _SHORT_CONTROLS)
            + 5 * sum(value.count(character) for character in _LONG_CONTROLS)
        )
        return size
    if isinstance(value, list):
        size = 2 + max(0, len(value) - 1)
        for item in value:
            size += canonical_size(item, remaining - size)
            if size > remaining:
                break
        return size
    if isinstance(value, dict):
        size = 2 + max(0, len(value) - 1)
        for name, item in value.items():
            size += len(name) + 3
            size += canonical_size(item, remaining - size)
            if size > remaining:
                break
        return size
    raise TypeError("VcsRef canonical values contain only strings and containers")


def canonical_bytes(value: JsonObject) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
