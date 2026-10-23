from collections.abc import Mapping
from typing import Annotated, Self, cast

from pydantic import (
    MISSING,
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    PrivateAttr,
    StrictStr,
    model_validator,
)
from pydantic.config import JsonDict
from pydantic_core import PydanticCustomError

from kora.ir.config import environment_schema
from kora.ir.contracts import TypeContract
from kora.ir.data import IrJsonObjectField, detach_ir_data, invalid_ir_data
from kora.ir.record import IrRecord, wire_name
from kora.ir.runtime import RuntimeSection, SecretsSection
from kora.ir.scalars import Digest, IrVersion, TypeContractName
from kora.ir.topology import Graph


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow", hide_input_in_errors=True)

    compiler: StrictStr | MISSING = MISSING
    source_digest: Digest | MISSING = MISSING
    presets: dict[StrictStr, IrJsonObjectField] | MISSING = MISSING
    defaults: IrJsonObjectField | MISSING = MISSING

    @model_validator(mode="wrap")
    @classmethod
    def read_data(cls, value: object, handler: ModelWrapValidatorHandler[Self]) -> Self:
        if isinstance(value, cls):
            return handler(value)
        if not isinstance(value, dict):
            raise invalid_ir_data()
        fields: dict[str, object] = {}
        for name, item in cast("dict[object, object]", value).items():
            if type(name) is not str:
                raise invalid_ir_data()
            fields[name] = item if name in cls.model_fields else detach_ir_data(item)
        return handler(fields)


def document_schema(schema: JsonDict) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("extensions", None)
    schema["patternProperties"] = {"^x-": {}}
    schema["additionalProperties"] = True


def inspect_ir_root(value: object) -> int:
    if not isinstance(value, Mapping):
        raise PydanticCustomError("ir_record_type", "the record is not an object")
    fields = {
        wire_name(name, field)
        for name, field in GraphDocument.model_fields.items()
        if name != "extensions"
    }
    return sum(
        not (type(key) is str and (key in fields or key.startswith("x-")))
        for key in cast("Mapping[object, object]", value)
    )


class GraphDocument(IrRecord):
    model_config = ConfigDict(json_schema_extra=document_schema)

    schema_: StrictStr | MISSING = Field(
        default=MISSING, alias="$schema", json_schema_extra={"format": "uri"}
    )
    ir_version: IrVersion
    meta: Provenance | MISSING = MISSING
    type_contracts: Annotated[
        dict[TypeContractName, TypeContract],
        Field(json_schema_extra=environment_schema),
    ]
    graph: Graph
    runtime: RuntimeSection
    secrets: SecretsSection | MISSING = MISSING
    _unknown_root_key_count: int = PrivateAttr(default=0)

    @property
    def unknown_root_key_count(self) -> int:
        return self._unknown_root_key_count

    @model_validator(mode="wrap")
    @classmethod
    def read_root(cls, value: object, handler: ModelWrapValidatorHandler[Self]) -> Self:
        if isinstance(value, cls):
            return handler(value)
        count = inspect_ir_root(value)
        fields = {
            wire_name(name, field)
            for name, field in cls.model_fields.items()
            if name != "extensions"
        }
        retained = {
            key: item
            for key, item in cast("Mapping[object, object]", value).items()
            if type(key) is str and (key in fields or key.startswith("x-"))
        }
        parsed = handler(retained)
        parsed._unknown_root_key_count = count
        return parsed
