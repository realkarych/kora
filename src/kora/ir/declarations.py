from typing import Annotated, Literal

from pydantic import MISSING, BeforeValidator, Field, StrictBool, StrictInt, StrictStr

from kora.ir.config import require_integer
from kora.ir.data import IrJsonObjectField
from kora.ir.record import IrRecord
from kora.ir.scalars import ArtifactType, InputSource, NodeId


class IrSourceLocation(IrRecord):
    file: StrictStr
    line: Annotated[StrictInt, Field(ge=1), BeforeValidator(require_integer)]


class InputRef(IrRecord):
    type: ArtifactType
    from_: InputSource | MISSING = Field(default=MISSING, alias="from")
    output: NodeId | MISSING = MISSING
    path: Annotated[StrictStr, Field(max_length=4096)] | MISSING = MISSING
    optional: StrictBool | MISSING = MISSING


class OutputDecl(IrRecord):
    type: ArtifactType
    schema_: IrJsonObjectField | MISSING = Field(default=MISSING, alias="schema")
    collect: NodeId | MISSING = MISSING


class SubgraphOutput(IrRecord):
    type: ArtifactType
    from_: NodeId = Field(alias="from")
    output: NodeId


class ParameterTarget(IrRecord):
    param: Annotated[StrictStr, Field(min_length=1)]


class OutputTarget(IrRecord):
    from_: NodeId = Field(alias="from")
    output: Annotated[StrictStr, Field(min_length=1)]
    path: Annotated[StrictStr, Field(min_length=1, max_length=4096)]


class TargetConfig(IrRecord):
    key: ParameterTarget | OutputTarget
    on_active: Literal["allow", "defer"] | MISSING = MISSING
