import math
import re
from typing import Annotated, Literal, cast

from pydantic import (
    MISSING,
    AfterValidator,
    BeforeValidator,
    Field,
    PlainValidator,
    StrictBool,
    StrictInt,
    StrictStr,
    WithJsonSchema,
)
from pydantic.config import JsonDict, JsonValue
from pydantic_core import PydanticCustomError

from kora.ir.node import ErrorClass
from kora.ir.record import IrRecord, require_array
from kora.ir.scalars import BranchTarget, Digest, Duration, EnvName, Expression, NodeId


def require_integer(value: object) -> int:
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value) and value.is_integer():
        return int(value)
    raise PydanticCustomError("ir_integer", "the field is not an integer")


def positive_number(value: object) -> int | float:
    if type(value) in (int, float):
        number = cast("int | float", value)
        if number > 0 and (isinstance(number, int) or math.isfinite(number)):
            return number
    raise PydanticCustomError(
        "ir_positive_number", "the field is not a positive finite number"
    )


def environment_schema(schema: JsonDict) -> None:
    patterns = schema.pop("patternProperties")
    pattern, values = next(iter(cast("dict[str, JsonValue]", patterns).items()))
    schema["propertyNames"] = {"type": "string", "pattern": pattern}
    schema["additionalProperties"] = values


def backoff_number(value: object) -> int | float:
    if type(value) in (int, float):
        number = cast("int | float", value)
        if number >= 1 and (isinstance(number, int) or math.isfinite(number)):
            return number
    raise PydanticCustomError(
        "ir_backoff_number", "the backoff is not a finite number of at least one"
    )


def unique_error_classes(values: tuple[ErrorClass, ...]) -> tuple[ErrorClass, ...]:
    if len(set(values)) != len(values):
        raise PydanticCustomError("ir_error_classes_unique", "error classes repeat")
    return values


JsonInteger = Annotated[StrictInt, BeforeValidator(require_integer)]
PositiveNumber = Annotated[
    int | float,
    PlainValidator(positive_number),
    WithJsonSchema({"type": "number", "exclusiveMinimum": 0}),
]
Strings = Annotated[tuple[StrictStr, ...], BeforeValidator(require_array)]
ResourceName = Annotated[
    StrictStr,
    Field(
        pattern=re.compile(
            r"^(?!/)(?![\s\S]*(^|/)\.{0,2}(/|$))[^\u0000-\u001f\u007f\\]+(?![\s\S])"
        )
    ),
]
ErrorClasses = Annotated[
    tuple[ErrorClass, ...],
    BeforeValidator(require_array),
    AfterValidator(unique_error_classes),
    Field(json_schema_extra={"uniqueItems": True}),
]
BackoffNumber = Annotated[
    int | float,
    PlainValidator(backoff_number),
    WithJsonSchema({"type": "number", "minimum": 1}),
]


class NodeLimits(IrRecord):
    timeout: Duration | MISSING = MISSING
    max_events: (
        Annotated[StrictInt, Field(ge=1, le=100000), BeforeValidator(require_integer)]
        | MISSING
    ) = MISSING
    budget_usd: PositiveNumber | MISSING = MISSING
    stall_after: Duration | MISSING = MISSING


class Workspace(IrRecord):
    repo: StrictStr
    rev: StrictStr | MISSING = MISSING
    from_ref: Annotated[StrictStr, Field(max_length=4096)] | MISSING = MISSING


class NetworkPolicy(IrRecord):
    egress: Strings | MISSING = MISSING


class McpServer(IrRecord):
    type: Literal["stdio", "http"]
    command: StrictStr | MISSING = MISSING
    args: Strings | MISSING = MISSING
    url: StrictStr | MISSING = MISSING
    env: dict[StrictStr, StrictStr] | MISSING = MISSING
    secret_env: dict[StrictStr, EnvName] | MISSING = MISSING
    network_policy: NetworkPolicy | MISSING = MISSING
    headers: dict[StrictStr, StrictStr] | MISSING = MISSING
    digest: Digest | MISSING = MISSING


class CommandResource(IrRecord):
    name: ResourceName
    sha256: Digest
    size: Annotated[StrictInt, Field(ge=0, le=262144), BeforeValidator(require_integer)]
    executable: StrictBool


class CommandConfig(IrRecord):
    cmd: Annotated[
        tuple[StrictStr, ...], Field(min_length=1), BeforeValidator(require_array)
    ]
    output_file: StrictStr | MISSING = MISSING
    resources: (
        Annotated[
            tuple[CommandResource, ...],
            Field(max_length=64),
            BeforeValidator(require_array),
        ]
        | MISSING
    ) = MISSING
    resource_set_digest: Digest | MISSING = MISSING
    workspace: Workspace
    env: (
        Annotated[dict[EnvName, StrictStr], Field(json_schema_extra=environment_schema)]
        | MISSING
    ) = MISSING
    secret_env: (
        Annotated[dict[EnvName, EnvName], Field(json_schema_extra=environment_schema)]
        | MISSING
    ) = MISSING
    network_policy: NetworkPolicy | MISSING = MISSING
    success_exit_codes: (
        Annotated[tuple[JsonInteger, ...], BeforeValidator(require_array)] | MISSING
    ) = MISSING
    runtime: StrictStr | MISSING = MISSING
    vcs: StrictStr | MISSING = MISSING


class BranchCase(IrRecord):
    when: Expression
    to: Annotated[
        tuple[BranchTarget, ...], Field(min_length=1), BeforeValidator(require_array)
    ]
    reason: Expression | MISSING = MISSING


class BranchConfig(IrRecord):
    cases: Annotated[
        tuple[BranchCase, ...], Field(min_length=1), BeforeValidator(require_array)
    ]
    else_: (
        Annotated[tuple[BranchTarget, ...], BeforeValidator(require_array)] | MISSING
    ) = Field(default=MISSING, alias="else")
    else_reason: Expression | MISSING = MISSING


class FanOutConfig(IrRecord):
    body: NodeId
    over: NodeId
    max_parallel: (
        Annotated[StrictInt, Field(ge=1), BeforeValidator(require_integer)] | MISSING
    ) = MISSING
    skip_failures: StrictBool | MISSING = MISSING


class SubgraphNodeConfig(IrRecord):
    body: NodeId


class Retry(IrRecord):
    max_attempts: (
        Annotated[StrictInt, Field(ge=1, le=100), BeforeValidator(require_integer)]
        | MISSING
    ) = MISSING
    initial_interval: Duration | MISSING = MISSING
    backoff_coefficient: BackoffNumber | MISSING = MISSING
    max_interval: Duration | MISSING = MISSING
    retry_on: ErrorClasses | MISSING = MISSING
    non_retryable: ErrorClasses | MISSING = MISSING
    predicate: Expression | MISSING = MISSING
    strategy: Literal["fresh", "resume", "fresh_with_feedback"] | MISSING = MISSING
