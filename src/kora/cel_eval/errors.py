from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NoReturn

Clock = Callable[[], datetime]


class ErrorKind(StrEnum):
    MISSING_FIELD = "missing_field"
    NO_OVERLOAD = "no_overload"
    INVALID_ARGUMENT = "invalid_argument"
    INDEX_OUT_OF_RANGE = "index_out_of_range"
    DIVISION_BY_ZERO = "division_by_zero"
    OVERFLOW = "overflow"
    BOUND_EXCEEDED = "bound_exceeded"
    NUMBER_OUT_OF_RANGE = "number_out_of_range"
    INVALID_VALUE = "invalid_value"
    WRONG_RESULT_TYPE = "wrong_result_type"
    PROJECTION_UNAVAILABLE = "projection_unavailable"
    INTERNAL_FAULT = "internal_fault"


class Fault(StrEnum):
    VALUE_ERROR = "value_error"
    RUNTIME_ERROR = "runtime_error"
    PANIC = "panic"
    UNMAPPED = "unmapped"


class UnavailableReason(StrEnum):
    BUSY = "busy"
    CLOSED = "closed"
    FAILED = "failed"
    CLOSE_TIMED_OUT = "close_timed_out"
    WRONG_LOOP = "wrong_loop"


@dataclass(frozen=True)
class EvaluationError:
    kind: ErrorKind
    field: str
    at: datetime
    offset: None = None
    event_id: str | None = None
    observed_at: datetime | None = None
    fault: Fault | None = None


@dataclass(frozen=True)
class ErrorContext:
    field: str
    event_id: str | None = None
    observed_at: datetime | None = None

    def record(
        self, kind: ErrorKind, clock: Clock, fault: Fault | None = None
    ) -> EvaluationError:
        return EvaluationError(
            kind, self.field, clock(), None, self.event_id, self.observed_at, fault
        )


class EvaluatorUnavailable(RuntimeError):
    def __init__(self, reason: UnavailableReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def raise_safe(error: Exception) -> NoReturn:
    try:
        raise error
    finally:
        error.__cause__ = None
        error.__context__ = None


def refuse(reason: UnavailableReason) -> NoReturn:
    raise_safe(EvaluatorUnavailable(reason))
