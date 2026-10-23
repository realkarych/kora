import math
from collections.abc import Iterator
from typing import Annotated, TypeAlias, cast

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic_core import CoreSchema, PydanticCustomError, core_schema

from kora.ir.json import JsonObject, JsonValue

RawObject: TypeAlias = dict[object, object]
RawArray: TypeAlias = list[object]


def invalid_ir_data() -> PydanticCustomError:
    return PydanticCustomError("ir_data_invalid", "the IR data is invalid")


def ir_data_scalar(value: object) -> None | bool | int | float | str:
    if value is None or type(value) is bool or type(value) is int or type(value) is str:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise invalid_ir_data()


def ir_data_entries(
    value: dict[object, object] | list[object],
) -> Iterator[tuple[object, object]]:
    if isinstance(value, dict):
        return iter(RawObject.items(value))
    return enumerate(RawArray.__iter__(value))


def detach_ir_data(value: object) -> JsonValue:
    source: dict[object, object] | list[object]
    result: JsonObject | list[JsonValue]
    if isinstance(value, dict):
        source = cast("dict[object, object]", value)
        result = {}
    elif isinstance(value, list):
        source = cast("list[object]", value)
        result = []
    else:
        return ir_data_scalar(value)
    entries = [ir_data_entries(source)]
    targets = [result]
    seen = {id(source)}
    while entries:
        entry = next(entries[-1], None)
        if entry is None:
            entries.pop()
            targets.pop()
            continue
        key, item = entry
        target = targets[-1]
        parsed: JsonValue
        if isinstance(item, dict) or isinstance(item, list):
            identity = id(cast("object", item))
            if identity in seen:
                raise invalid_ir_data()
            seen.add(identity)
            if isinstance(item, dict):
                source = cast("dict[object, object]", item)
                parsed = {}
            else:
                source = cast("list[object]", item)
                parsed = []
            entries.append(ir_data_entries(source))
            targets.append(parsed)
        else:
            parsed = ir_data_scalar(item)
        if isinstance(target, dict):
            if type(key) is not str:
                raise invalid_ir_data()
            target[key] = parsed
        else:
            target.append(parsed)
    return result


def validate_ir_json_value(value: object) -> JsonValue:
    return detach_ir_data(value)


class IrDataValue:
    def __get_pydantic_core_schema__(
        self, source: object, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_plain_validator_function(validate_ir_json_value)

    def __get_pydantic_json_schema__(
        self, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> dict[str, str]:
        return {}


IrJsonValueField = Annotated[JsonValue, IrDataValue()]


class IrJsonObjectField(dict[str, JsonValue]):
    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[object], handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_plain_validator_function(validate_ir_json_object)

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> dict[str, str]:
        return {"type": "object"}


def validate_ir_json_object(value: object) -> IrJsonObjectField:
    if not isinstance(value, dict):
        raise invalid_ir_data()
    return IrJsonObjectField(cast("JsonObject", detach_ir_data(cast("object", value))))
