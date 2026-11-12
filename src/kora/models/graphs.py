from typing import Annotated, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    StrictBool,
    StrictInt,
    StrictStr,
)
from pydantic_core import CoreSchema, PydanticCustomError, core_schema

from kora.ir.diagnostic_collection import MAX_DIAGNOSTICS
from kora.ir.diagnostics import Diagnostic
from kora.ir.json import JsonValue
from kora.ir.scalars import NodeId
from kora.models.errors import WireRecord

GRAPH_VERSION_PREFIX = "gv_"
Sha256Hex = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class OpaqueObject(dict[str, JsonValue]):
    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[object], handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_plain_validator_function(cls.validated)

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> dict[str, str]:
        return {"type": "object"}

    @classmethod
    def validated(cls, value: object) -> Self:
        if type(value) is not dict and type(value) is not cls:
            raise PydanticCustomError(
                "opaque_object", "the value must be a JSON object"
            )
        return cls(cast("dict[str, JsonValue]", value))


class DeployRequest(WireRecord):
    graph: NodeId
    ir: OpaqueObject
    resources: Annotated[tuple[OpaqueObject, ...], Field(max_length=0)]


class DeployResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)

    graph_version_id: Annotated[
        StrictStr, Field(pattern=r"^gv_[0-9A-HJKMNP-TV-Z]{26}$")
    ]
    number: Annotated[StrictInt, Field(ge=1)]
    ir_digest: Sha256Hex
    execution_contract_digest: Sha256Hex
    deployment_digest: Sha256Hex
    reused: StrictBool
    warnings: tuple[Diagnostic, ...] = Field(max_length=MAX_DIAGNOSTICS)
