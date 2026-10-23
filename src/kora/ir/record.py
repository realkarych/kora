from collections.abc import Mapping
from typing import ClassVar, LiteralString, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    ModelWrapValidatorHandler,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)
from pydantic.config import JsonDict
from pydantic.fields import FieldInfo
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, PydanticCustomError

from kora.ir.json import JsonObjectField


def extension_schema(schema: JsonDict) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("extensions", None)
    schema["patternProperties"] = {"^x-": {}}
    schema["additionalProperties"] = False


def wire_name(name: str, field: FieldInfo) -> str:
    return field.alias if field.alias is not None else name


def require_array(value: object) -> object:
    if not isinstance(value, list | tuple):
        raise PydanticCustomError("ir_array", "the field is not an array")
    return cast("list[object] | tuple[object, ...]", value)


class IrRecord(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
        json_schema_extra=extension_schema,
        json_schema_mode_override="validation",
    )

    shape_error: ClassVar[tuple[LiteralString, LiteralString]] = (
        "ir_record_type",
        "the record is not an object",
    )
    field_error: ClassVar[tuple[LiteralString, LiteralString]] = (
        "ir_record_field_unknown",
        "the record has an unknown field",
    )
    extensions: JsonObjectField = Field(default_factory=JsonObjectField)

    @model_validator(mode="wrap")
    @classmethod
    def read_extensions(
        cls, value: object, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        if isinstance(value, cls):
            return handler(value)
        if not isinstance(value, Mapping):
            raise PydanticCustomError(*cls.shape_error)
        fields: dict[str, object] = {}
        extensions: dict[str, object] = {}
        wire_fields = {
            wire_name(name, field)
            for name, field in cls.model_fields.items()
            if name != "extensions"
        }
        for key, item in cast("Mapping[object, object]", value).items():
            if type(key) is str and key.startswith("x-"):
                extensions[key] = item
            elif type(key) is str and key in wire_fields:
                fields[key] = item
            else:
                raise PydanticCustomError(*cls.field_error)
        fields["extensions"] = extensions
        return handler(fields)

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        if handler.mode == "serialization":
            core_schema = without_record_serializer(core_schema)
        return handler(core_schema)

    @model_serializer(mode="wrap")
    def write_extensions(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        serialised: object = handler(self)
        data = dict(cast("dict[str, object]", serialised))
        extensions = data.pop("extensions", {})
        return {**data, **cast("dict[str, object]", extensions)}


def without_record_serializer(schema: CoreSchema) -> CoreSchema:
    if (
        schema["type"] == "function-wrap"
        or schema["type"] == "function-after"
        or schema["type"] == "function-before"
    ):
        copied = schema.copy()
        copied["schema"] = without_record_serializer(schema["schema"])
        return copied
    if schema["type"] == "model":
        model = schema.copy()
        model.pop("serialization", None)
        return model
    return schema
