import math
from typing import Annotated, cast

from pydantic import (
    MISSING,
    BeforeValidator,
    Field,
    PlainValidator,
    StrictInt,
    StrictStr,
    WithJsonSchema,
)
from pydantic_core import PydanticCustomError

from kora.ir.config import (
    NodeLimits,
    Retry,
    Strings,
    environment_schema,
    require_integer,
)
from kora.ir.data import IrJsonObjectField
from kora.ir.record import IrRecord
from kora.ir.scalars import Duration, EnvName, RetentionPeriod, SecretRef


def headroom_number(value: object) -> int | float:
    if type(value) in (int, float):
        number = cast("int | float", value)
        if 0 <= number <= 100 and (isinstance(number, int) or math.isfinite(number)):
            return number
    raise PydanticCustomError(
        "ir_headroom_number",
        "the headroom is not a finite number from zero to one hundred",
    )


HeadroomNumber = Annotated[
    int | float,
    PlainValidator(headroom_number),
    WithJsonSchema({"type": "number", "minimum": 0, "maximum": 100}),
]


class Concurrency(IrRecord):
    max_running_nodes: (
        Annotated[StrictInt, Field(ge=1), BeforeValidator(require_integer)] | MISSING
    ) = MISSING
    max_running_runs: (
        Annotated[StrictInt, Field(ge=1), BeforeValidator(require_integer)] | MISSING
    ) = MISSING


class Retention(IrRecord):
    transcripts: RetentionPeriod | MISSING = MISSING
    artifact_payloads: RetentionPeriod | MISSING = MISSING


class ProviderRetry(IrRecord):
    max_wait: Duration | MISSING = MISSING


class RuntimeSection(IrRecord):
    runtime: StrictStr
    vcs: StrictStr
    artifact_storage: StrictStr | MISSING = MISSING
    approval_channels: dict[StrictStr, IrJsonObjectField] | MISSING = MISSING
    concurrency: Concurrency | MISSING = MISSING
    limits: NodeLimits | MISSING = MISSING
    budget_headroom_pct: HeadroomNumber | MISSING = MISSING
    stall_after: Duration | MISSING = MISSING
    retention: Retention | MISSING = MISSING
    protected_paths: Strings | MISSING = Field(
        default=MISSING,
        description=(
            "path globs (ir.md section 4.1 syntax) of gate files agent nodes "
            "may not edit and command gates verify against the base commit (DD-34)"
        ),
    )
    infrastructure_retry: Retry | MISSING = MISSING
    provider_retry: ProviderRetry | MISSING = MISSING


class SecretsSection(IrRecord):
    aliases: dict[EnvName, SecretRef] | MISSING = Field(
        default=MISSING, json_schema_extra=environment_schema
    )
