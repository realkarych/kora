import math
from typing import TypeAlias, cast

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = dict[str, JsonValue]

DEFAULT_MAX_DEPTH: int = 16
JSON_RULES: frozenset[str] = frozenset(
    {"json_object", "json_depth", "json_key", "json_float", "json_scalar"}
)


class JsonShapeError(ValueError):
    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


def parse_json_object(value: object, max_depth: int = DEFAULT_MAX_DEPTH) -> JsonObject:
    if not isinstance(value, dict):
        raise JsonShapeError("json_object")
    return parse_object(cast("dict[object, object]", value), max_depth, 1)


def parse_object(value: dict[object, object], max_depth: int, depth: int) -> JsonObject:
    if depth > max_depth:
        raise JsonShapeError("json_depth")
    parsed: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise JsonShapeError("json_key")
        parsed[key] = parse_value(item, max_depth, depth)
    return parsed


def parse_value(value: object, max_depth: int, depth: int) -> JsonValue:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JsonShapeError("json_float")
        return value
    if isinstance(value, list):
        if depth + 1 > max_depth:
            raise JsonShapeError("json_depth")
        return [
            parse_value(item, max_depth, depth + 1)
            for item in cast("list[object]", value)
        ]
    if isinstance(value, dict):
        return parse_object(cast("dict[object, object]", value), max_depth, depth + 1)
    raise JsonShapeError("json_scalar")
