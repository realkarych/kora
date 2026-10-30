from typing import cast

from kora.ir.json import JsonObject, JsonValue


def matches_fixed(value: object, expected: JsonValue) -> bool:
    if type(value) is not type(expected):
        return False
    if type(expected) is dict:
        supplied = cast("dict[object, object]", value)
        target = cast("JsonObject", expected)
        if len(supplied) != len(target):
            return False
        if any(type(name) is not str for name in supplied):
            return False
        return all(
            name in supplied and matches_fixed(supplied[name], item)
            for name, item in target.items()
        )
    if type(expected) is list:
        supplied_items = cast("list[object]", value)
        target_items = cast("list[JsonValue]", expected)
        return len(supplied_items) == len(target_items) and all(
            matches_fixed(supplied_items[index], item)
            for index, item in enumerate(target_items)
        )
    return value == expected
