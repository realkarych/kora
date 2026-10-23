import math
from typing import Annotated, cast

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic_core import CoreSchema, PydanticCustomError, core_schema

from kora.ir.json import JsonObject, JsonValue


def invalid_ir_data() -> PydanticCustomError:
    return PydanticCustomError("ir_data_invalid", "the IR data is invalid")


def detach_ir_data(value: object) -> JsonValue:
    result: list[JsonValue] = [None]
    pending: list[tuple[object, JsonObject | list[JsonValue], str | int]] = [
        (value, result, 0)
    ]
    seen: set[int] = set()
    while pending:
        source, target, key = pending.pop()
        parsed: JsonValue
        if source is None or type(source) in (bool, int, str):
            parsed = cast("None | bool | int | str", source)
        elif type(source) is float:
            number = source
            if not math.isfinite(number):
                raise invalid_ir_data()
            parsed = number
        elif isinstance(source, dict | list):
            identity = id(cast("object", source))
            if identity in seen:
                raise invalid_ir_data()
            seen.add(identity)
            if isinstance(source, dict):
                copied: JsonObject = {}
                children: list[
                    tuple[object, JsonObject | list[JsonValue], str | int]
                ] = []
                for name, item in dict[object, object].items(
                    cast("dict[object, object]", source)
                ):
                    if type(name) is not str:
                        raise invalid_ir_data()
                    copied[name] = None
                    children.append((item, copied, name))
                pending.extend(reversed(children))
                parsed = copied
            else:
                items = cast("list[object]", source)
                array: list[JsonValue] = [None] * len(items)
                pending.extend(
                    (items[index], array, index)
                    for index in range(len(items) - 1, -1, -1)
                )
                parsed = array
        else:
            raise invalid_ir_data()
        if isinstance(target, dict):
            target[cast("str", key)] = parsed
        else:
            target[cast("int", key)] = parsed
    return result[0]


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
