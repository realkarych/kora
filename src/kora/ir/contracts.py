from typing import Annotated

from pydantic import AfterValidator, Field, StrictStr
from pydantic_core import PydanticCustomError

from kora.ir.data import IrJsonObjectField
from kora.ir.record import IrRecord
from kora.ir.scalars import TypeContractName

SCHEMA_DIALECT: str = "https://json-schema.org/draft/2020-12/schema"


def require_schema_dialect(value: IrJsonObjectField) -> IrJsonObjectField:
    if value.get("$schema") != SCHEMA_DIALECT:
        raise PydanticCustomError("ir_schema_dialect", "the schema dialect is invalid")
    return value


ContractSchema = Annotated[
    IrJsonObjectField,
    AfterValidator(require_schema_dialect),
    Field(
        json_schema_extra={
            "required": ["$schema"],
            "properties": {"$schema": {"const": SCHEMA_DIALECT}},
        }
    ),
]


class TypeBehavior(IrRecord):
    validation: Annotated[StrictStr, Field(min_length=1)]
    digest_fields: Annotated[StrictStr, Field(min_length=1)]
    summary: Annotated[StrictStr, Field(min_length=1)]


class BuiltinTypeContract(IrRecord):
    builtin_revision: Annotated[StrictStr, Field(min_length=1)]


class CustomTypeContract(IrRecord):
    schema_: ContractSchema = Field(alias="schema")
    parent: TypeContractName | None
    behavior: TypeBehavior


TypeContract = BuiltinTypeContract | CustomTypeContract
