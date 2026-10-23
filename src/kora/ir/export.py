import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from json.encoder import encode_basestring_ascii
from typing import TypeAlias, cast, get_args, get_origin

from pydantic import MISSING
from pydantic_core import to_json

from kora.ir.data import IrDataValue, IrJsonObjectField, detach_ir_data, invalid_ir_data
from kora.ir.document import Provenance
from kora.ir.json import JsonObject, JsonValue, parse_json_object
from kora.ir.record import IrRecord, wire_name

JsonArray: TypeAlias = list[JsonValue]


@dataclass
class Visit:
    value: object
    target: JsonObject | list[JsonValue]
    key: str | int


@dataclass
class Leave:
    identity: int


def store_value(
    target: JsonObject | list[JsonValue], key: str | int, value: JsonValue
) -> None:
    if isinstance(target, dict):
        target[cast("str", key)] = value
    else:
        target[cast("int", key)] = value


def is_ir_data_annotation(annotation: object) -> bool:
    pending: list[object] = [annotation]
    while pending:
        item = pending.pop()
        if isinstance(item, IrDataValue):
            return True
        pending.extend(get_args(item))
    return False


def is_ir_object_map_annotation(annotation: object) -> bool:
    if get_origin(annotation) is dict:
        arguments = get_args(annotation)
        return len(arguments) == 2 and arguments[1] is IrJsonObjectField
    return any(is_ir_object_map_annotation(item) for item in get_args(annotation))


def export_ir_object_map(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise invalid_ir_data()
    result: JsonObject = {}
    for name, item in dict[object, object].items(cast("dict[object, object]", value)):
        if type(name) is not str or not isinstance(item, dict):
            raise invalid_ir_data()
        result[name] = detach_ir_data(cast("object", item))
    return result


def require_ir_root(value: object) -> IrRecord | Provenance:
    if not isinstance(value, IrRecord | Provenance):
        raise invalid_ir_data()
    return value


def export_ir(root: IrRecord | Provenance) -> JsonObject:
    root = require_ir_root(root)
    result: list[JsonValue] = [None]
    stack: list[Visit | Leave] = [Visit(root, result, 0)]
    active: set[int] = set()
    while stack:
        task = stack.pop()
        if isinstance(task, Leave):
            active.remove(task.identity)
            continue
        value = task.value
        if isinstance(value, Enum):
            value = value.value
        if value is None or type(value) in (bool, int, str):
            store_value(task.target, task.key, cast("JsonValue", value))
            continue
        if type(value) is float:
            if not math.isfinite(value):
                raise invalid_ir_data()
            store_value(task.target, task.key, value)
            continue
        identity = id(value)
        if identity in active:
            raise invalid_ir_data()
        active.add(identity)
        stack.append(Leave(identity))
        if isinstance(value, IrRecord | Provenance):
            data: JsonObject = {}
            store_value(task.target, task.key, data)
            for name, field in type(value).model_fields.items():
                if name == "extensions" or name not in value.model_fields_set:
                    continue
                item: object = getattr(value, name)
                if item is MISSING:
                    continue
                alias = wire_name(name, field)
                data[alias] = None
                if isinstance(item, IrJsonObjectField) or is_ir_data_annotation(
                    field.annotation
                ):
                    data[alias] = detach_ir_data(item)
                elif isinstance(item, dict) and is_ir_object_map_annotation(
                    field.annotation
                ):
                    data[alias] = export_ir_object_map(cast("object", item))
                else:
                    stack.append(Visit(cast("object", item), data, alias))
            if isinstance(value, IrRecord):
                extensions = parse_json_object(value.extensions)
                if any(not name.startswith("x-") for name in extensions):
                    raise invalid_ir_data()
                data.update(extensions)
            elif value.__pydantic_extra__ is not None:
                for name, item in cast(
                    "dict[str, object]", value.__pydantic_extra__
                ).items():
                    if type(name) is not str:
                        raise invalid_ir_data()
                    data[name] = detach_ir_data(item)
        elif isinstance(value, dict):
            data = {}
            store_value(task.target, task.key, data)
            for key, item in dict[object, object].items(
                cast("dict[object, object]", value)
            ):
                if type(key) is not str:
                    raise invalid_ir_data()
                data[key] = None
                stack.append(Visit(item, data, key))
        elif isinstance(value, list | tuple):
            items = cast("list[object] | tuple[object, ...]", value)
            array: list[JsonValue] = [None] * len(items)
            store_value(task.target, task.key, array)
            stack.extend(Visit(item, array, index) for index, item in enumerate(items))
        else:
            raise invalid_ir_data()
    return cast("JsonObject", result[0])


def render_entries(
    value: JsonObject | list[JsonValue],
) -> Iterator[tuple[str | int, JsonValue]]:
    if isinstance(value, dict):
        return iter(JsonObject.items(value))
    return enumerate(JsonArray.__iter__(value))


def render_ir_json(root: IrRecord | Provenance) -> str:
    data = export_ir(root)
    fragments = ["{"]
    entries = [render_entries(data)]
    endings = ["}"]
    first = [True]
    while entries:
        entry = next(entries[-1], None)
        if entry is None:
            entries.pop()
            first.pop()
            fragments.append(endings.pop())
            continue
        key, value = entry
        if first[-1]:
            first[-1] = False
        else:
            fragments.append(",")
        if endings[-1] == "}":
            fragments.append(encode_basestring_ascii(cast("str", key)))
            fragments.append(":")
        if isinstance(value, dict) or isinstance(value, list):
            opening, ending = ("{", "}") if isinstance(value, dict) else ("[", "]")
            fragments.append(opening)
            entries.append(render_entries(value))
            endings.append(ending)
            first.append(True)
        elif value is None:
            fragments.append("null")
        elif value is True:
            fragments.append("true")
        elif value is False:
            fragments.append("false")
        elif isinstance(value, str):
            fragments.append(encode_basestring_ascii(value))
        elif type(value) is int:
            fragments.append(to_json(value).decode("ascii"))
        else:
            fragments.append(
                json.dumps(
                    value, ensure_ascii=True, allow_nan=False, separators=(",", ":")
                )
            )
    return "".join(fragments)
