from enum import StrEnum
from typing import ClassVar, LiteralString, cast

from pydantic import StrictStr, field_validator
from pydantic_core import PydanticCustomError

from kora.ir.record import IrRecord


class ErrorClass(StrEnum):
    HARNESS_ERROR = "harness_error"
    COMMAND_FAILED = "command_failed"
    TIMEOUT = "timeout"
    EVENTS_EXHAUSTED = "events_exhausted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ARTIFACT_INVALID = "artifact_invalid"
    ATTEMPT_FAILED = "attempt_failed"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_EXPIRED = "approval_expired"
    CANDIDATE_MOVED = "candidate_moved"
    WAIT_EXPIRED = "wait_expired"
    STALLED = "stalled"
    PROVIDER_LIMITED = "provider_limited"
    PROTECTED_PATH_MODIFIED = "protected_path_modified"
    CREDENTIAL_LEAK = "secret_leak"
    INTEGRITY_ERROR = "integrity_error"
    INFRASTRUCTURE = "infrastructure"
    CANCELLED = "cancelled"
    CONFIG_ERROR = "config_error"


class ToolPolicy(IrRecord):
    shape_error: ClassVar[tuple[LiteralString, LiteralString]] = (
        "tool_policy_type",
        "the tool policy is not an object",
    )
    field_error: ClassVar[tuple[LiteralString, LiteralString]] = (
        "tool_policy_field_unknown",
        "the tool policy has an unknown field",
    )

    allow: tuple[StrictStr, ...] = ()
    deny: tuple[StrictStr, ...] = ()

    @field_validator("allow", "deny", mode="before")
    @classmethod
    def require_sequence(cls, value: object) -> object:
        if not isinstance(value, list | tuple):
            raise PydanticCustomError(
                "tool_policy_patterns", "the patterns are not a list"
            )
        return cast("list[object] | tuple[object, ...]", value)
