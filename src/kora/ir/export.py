import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import cast, get_args

from pydantic import MISSING
from pydantic_core import to_json

from kora.ir.data import IrDataValue, IrJsonObjectField, detach_ir_data, invalid_ir_data
from kora.ir.json import JsonObject, JsonValue, parse_json_object
from kora.ir.record import IrRecord, wire_name


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


def require_ir_root(value: object) -> IrRecord:
    if not isinstance(value, IrRecord):
        raise invalid_ir_data()
    return value


def export_ir(root: IrRecord) -> JsonObject:
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
        if isinstance(value, IrRecord):
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
                else:
                    stack.append(Visit(item, data, alias))
            extensions = parse_json_object(value.extensions)
            if any(not name.startswith("x-") for name in extensions):
                raise invalid_ir_data()
            data.update(extensions)
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


def render_ir_json(root: IrRecord) -> str:
    data = export_ir(root)
    fragments: list[str] = []
    pending: list[tuple[bool, JsonValue]] = [(False, data)]
    while pending:
        literal, value = pending.pop()
        if literal:
            fragments.append(cast("str", value))
        elif isinstance(value, dict):
            fragments.append("{")
            pending.append((True, "}"))
            entries = list(value.items())
            for index in range(len(entries) - 1, -1, -1):
                key, item = entries[index]
                pending.append((False, item))
                pending.append((True, ":"))
                pending.append((True, json.dumps(key, ensure_ascii=True)))
                if index:
                    pending.append((True, ","))
        elif isinstance(value, list):
            fragments.append("[")
            pending.append((True, "]"))
            for index in range(len(value) - 1, -1, -1):
                pending.append((False, value[index]))
                if index:
                    pending.append((True, ","))
        elif type(value) is int:
            fragments.append(to_json(value).decode("ascii"))
        else:
            fragments.append(
                json.dumps(
                    value, ensure_ascii=True, allow_nan=False, separators=(",", ":")
                )
            )
    return "".join(fragments)
