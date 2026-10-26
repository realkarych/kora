from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    PlainValidator,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)
from pydantic_core import PydanticCustomError

from kora.ir import ErrorClass
from kora.ir.json import JsonObjectField
from kora.protocols.common import BoundaryModel, budgeted_object
from kora.protocols.evidence import ClosureEvidence, Code, TerminationReceipt
from kora.protocols.identity import encoded_size

MAX_ERROR_BYTES: Final[int] = 16_384
MAX_MESSAGE_BYTES: Final[int] = 4096


def report_refused() -> PydanticCustomError:
    return PydanticCustomError("report_invalid", "the report is invalid")


def printable_message(value: str) -> str:
    if not value.isprintable():
        raise report_refused()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise report_refused() from None
    if size > MAX_MESSAGE_BYTES:
        raise report_refused()
    return value


def bounded_details(value: object) -> JsonObjectField:
    parsed = budgeted_object(value, MAX_ERROR_BYTES)
    if parsed is None:
        raise report_refused()
    return parsed


Message = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=MAX_MESSAGE_BYTES),
    AfterValidator(printable_message),
]
ErrorDetails = Annotated[
    JsonObjectField,
    PlainValidator(bounded_details),
    WithJsonSchema({"type": "object"}),
]
LostReason = Literal["execution_gone", "checkpoint_missing", "result_unrecoverable"]


class AttemptError(BoundaryModel):
    code: Code
    message: Message | None
    details: ErrorDetails | None

    @model_validator(mode="after")
    def bounded_error(self) -> Self:
        if encoded_size(self) > MAX_ERROR_BYTES:
            raise report_refused()
        return self


class TerminalReport(BoundaryModel):
    outcome: Literal["succeeded", "failed", "cancelled", "lost"]
    error_class: ErrorClass | None
    error: AttemptError | None
    lost_reason: LostReason | None
    evidence: ClosureEvidence

    @model_validator(mode="after")
    def shape_matches_outcome(self) -> Self:
        failed = self.outcome == "failed"
        if failed != (self.error_class is not None) or failed != (
            self.error is not None
        ):
            raise report_refused()
        if (self.outcome == "lost") != (self.lost_reason is not None):
            raise report_refused()
        if self.evidence.publication is None or self.evidence.runtime is None:
            raise report_refused()
        if self.outcome in {"cancelled", "lost"} and not isinstance(
            self.evidence.runtime, TerminationReceipt
        ):
            raise report_refused()
        return self
