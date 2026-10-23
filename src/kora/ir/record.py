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
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, PydanticCustomError

from kora.ir.json import JsonObjectField


def extension_schema(schema: JsonDict) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("extensions", None)
    schema["patternProperties"] = {"^x-": {}}
    schema["additionalProperties"] = False


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
            field.alias if field.alias is not None else name
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
        if handler.mode == "serialization" and core_schema["type"] == "function-wrap":
            core_schema = core_schema.copy()
            model_schema = core_schema["schema"].copy()
            model_schema.pop("serialization", None)
            core_schema["schema"] = model_schema
        return handler(core_schema)

    @model_serializer(mode="wrap")
    def write_extensions(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        serialised: object = handler(self)
        data = dict(cast("dict[str, object]", serialised))
        extensions = data.pop("extensions", {})
        return {**data, **cast("dict[str, object]", extensions)}
