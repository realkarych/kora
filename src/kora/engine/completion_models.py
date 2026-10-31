from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias

from kora.ir import ErrorClass
from kora.protocols.deadlines import (
    ExecutionDeadline,
    ScopeKind,
    is_scope_identifier,
    is_scope_kind,
    is_utc,
)

AttemptStatus: TypeAlias = Literal[
    "created",
    "claimed",
    "submitted",
    "running",
    "finishing",
    "succeeded",
    "failed",
    "cancelled",
    "lost",
]
RunStatus: TypeAlias = Literal["pending", "running", "succeeded", "failed", "cancelled"]
CancelCause: TypeAlias = Literal["cancel", "clear", "supersede", "timeout", "stop"]
ReportOutcome: TypeAlias = Literal["succeeded", "failed", "cancelled", "lost"]
PublicationState: TypeAlias = Literal[
    "not_applicable", "not_dispatched", "unknown", "completed", "failed"
]
OutputVerification: TypeAlias = Literal[
    "pending", "complete_valid", "invalid_or_incomplete"
]
Prerequisite: TypeAlias = Literal[
    "closure", "deadlines", "report", "outputs", "top_level"
]

ATTEMPT_STATUSES: frozenset[str] = frozenset(
    {
        "created",
        "claimed",
        "submitted",
        "running",
        "finishing",
        "succeeded",
        "failed",
        "cancelled",
        "lost",
    }
)
RUN_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "succeeded", "failed", "cancelled"}
)
CANCEL_CAUSES: frozenset[str] = frozenset(
    {"cancel", "clear", "supersede", "timeout", "stop"}
)
REPORT_OUTCOMES: frozenset[str] = frozenset(
    {"succeeded", "failed", "cancelled", "lost"}
)
PUBLICATION_STATES: frozenset[str] = frozenset(
    {"not_applicable", "not_dispatched", "unknown", "completed", "failed"}
)
OUTPUT_VERIFICATIONS: frozenset[str] = frozenset(
    {"pending", "complete_valid", "invalid_or_incomplete"}
)
PREREQUISITES: frozenset[str] = frozenset(
    {"closure", "deadlines", "report", "outputs", "top_level"}
)
WORKER_ERROR_CLASSES: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.HARNESS_ERROR,
        ErrorClass.COMMAND_FAILED,
        ErrorClass.TIMEOUT,
        ErrorClass.EVENTS_EXHAUSTED,
        ErrorClass.BUDGET_EXHAUSTED,
        ErrorClass.ARTIFACT_INVALID,
        ErrorClass.STALLED,
        ErrorClass.PROVIDER_LIMITED,
        ErrorClass.PROTECTED_PATH_MODIFIED,
        ErrorClass.CREDENTIAL_LEAK,
        ErrorClass.INTEGRITY_ERROR,
        ErrorClass.INFRASTRUCTURE,
        ErrorClass.CONFIG_ERROR,
    }
)


def require_completion_fact(condition: bool) -> None:
    if not condition:
        raise ValueError("completion facts are invalid")


def require_literal(value: object, allowed: frozenset[str]) -> None:
    require_completion_fact(type(value) is str and value in allowed)


def require_booleans(*values: bool) -> None:
    require_completion_fact(all(type(value) is bool for value in values))


def require_instance(
    value: object, expected: type[object] | tuple[type[object], ...]
) -> None:
    require_completion_fact(isinstance(value, expected))


@dataclass(frozen=True)
class ExpiryOwner:
    scope_kind: ScopeKind
    scope_id: str
    at: datetime

    def __post_init__(self) -> None:
        require_completion_fact(is_scope_kind(self.scope_kind))
        require_completion_fact(is_scope_identifier(self.scope_id))
        require_completion_fact(is_utc(self.at))


@dataclass(frozen=True)
class DeadlineFactsUnavailable:
    pass


@dataclass(frozen=True)
class UnlatchedExpiries:
    observed_at: datetime
    deadlines: tuple[ExecutionDeadline, ...]

    def __post_init__(self) -> None:
        require_completion_fact(is_utc(self.observed_at))
        require_completion_fact(type(self.deadlines) is tuple and bool(self.deadlines))
        for deadline in self.deadlines:
            require_instance(deadline, ExecutionDeadline)
        require_completion_fact(
            all(self.observed_at >= deadline.at for deadline in self.deadlines)
        )
        owners = {
            (deadline.scope_kind, deadline.scope_id) for deadline in self.deadlines
        }
        require_completion_fact(len(owners) == len(self.deadlines))


@dataclass(frozen=True)
class CheckedDeadlineState:
    own_expiry_latched: bool
    enclosing_scopes_eligible: bool

    def __post_init__(self) -> None:
        require_booleans(self.own_expiry_latched, self.enclosing_scopes_eligible)


DeadlineEvaluation: TypeAlias = (
    DeadlineFactsUnavailable | UnlatchedExpiries | CheckedDeadlineState
)


@dataclass(frozen=True)
class ClosureFacts:
    runtime_quiescent: bool
    all_dispatchers_closed: bool
    publication_state: PublicationState

    def __post_init__(self) -> None:
        require_booleans(self.runtime_quiescent, self.all_dispatchers_closed)
        require_literal(self.publication_state, PUBLICATION_STATES)


@dataclass(frozen=True)
class ReportFactsUnavailable:
    pass


@dataclass(frozen=True)
class NoRecordedReport:
    pass


@dataclass(frozen=True)
class RecordedReportFacts:
    outcome: ReportOutcome
    error_class: ErrorClass | None
    shape_validated: bool
    admission_validated: bool
    retirement_verified: bool

    def __post_init__(self) -> None:
        require_literal(self.outcome, REPORT_OUTCOMES)
        require_booleans(
            self.shape_validated, self.admission_validated, self.retirement_verified
        )
        require_completion_fact(
            isinstance(self.error_class, ErrorClass)
            and self.error_class in WORKER_ERROR_CLASSES
            if self.outcome == "failed"
            else self.error_class is None
        )


ReportEvaluation: TypeAlias = (
    ReportFactsUnavailable | NoRecordedReport | RecordedReportFacts
)


@dataclass(frozen=True)
class AttemptCompletionFacts:
    status: AttemptStatus
    is_current_attempt: bool
    desired_cancel: bool
    stop_requested: bool
    cancel_cause: CancelCause | None
    operator_attested_closure: bool
    closure: ClosureFacts | None
    report: ReportEvaluation
    outputs: OutputVerification
    deadlines: DeadlineEvaluation

    def __post_init__(self) -> None:
        require_literal(self.status, ATTEMPT_STATUSES)
        require_booleans(
            self.is_current_attempt,
            self.desired_cancel,
            self.stop_requested,
            self.operator_attested_closure,
        )
        if self.cancel_cause is not None:
            require_literal(self.cancel_cause, CANCEL_CAUSES)
        if self.closure is not None:
            require_instance(self.closure, ClosureFacts)
        require_instance(
            self.report, (ReportFactsUnavailable, NoRecordedReport, RecordedReportFacts)
        )
        require_completion_fact(
            self.status != "finishing" or not isinstance(self.report, NoRecordedReport)
        )
        require_literal(self.outputs, OUTPUT_VERIFICATIONS)
        require_instance(
            self.deadlines,
            (DeadlineFactsUnavailable, UnlatchedExpiries, CheckedDeadlineState),
        )


@dataclass(frozen=True)
class RunCompletionFacts:
    run_id: str
    status: RunStatus
    observed_at: datetime
    deadline_at: datetime | None
    deadline_expired_at: datetime | None
    deadline_checked: bool
    top_level_checked: bool
    has_unfinished_top_level: bool
    has_failed_top_level: bool

    def __post_init__(self) -> None:
        require_completion_fact(is_scope_identifier(self.run_id))
        require_literal(self.status, RUN_STATUSES)
        require_completion_fact(is_utc(self.observed_at))
        require_completion_fact(self.deadline_at is None or is_utc(self.deadline_at))
        require_completion_fact(
            self.deadline_expired_at is None or is_utc(self.deadline_expired_at)
        )
        require_booleans(
            self.deadline_checked,
            self.top_level_checked,
            self.has_unfinished_top_level,
            self.has_failed_top_level,
        )


@dataclass(frozen=True)
class NoCompletion:
    pass


@dataclass(frozen=True)
class AwaitCompletionPrerequisite:
    reason: Prerequisite

    def __post_init__(self) -> None:
        require_literal(self.reason, PREREQUISITES)


@dataclass(frozen=True)
class ExpiryTransitionsRequired:
    owners: tuple[ExpiryOwner, ...]

    def __post_init__(self) -> None:
        require_completion_fact(type(self.owners) is tuple and bool(self.owners))
        for owner in self.owners:
            require_instance(owner, ExpiryOwner)
        require_completion_fact(
            len({(owner.scope_kind, owner.scope_id) for owner in self.owners})
            == len(self.owners)
        )


@dataclass(frozen=True)
class AttemptTerminalDecision:
    status: ReportOutcome
    error_class: ErrorClass | None
    affects_current_instance: bool

    def __post_init__(self) -> None:
        require_literal(self.status, REPORT_OUTCOMES)
        require_booleans(self.affects_current_instance)
        require_completion_fact(
            isinstance(self.error_class, ErrorClass)
            and self.error_class in WORKER_ERROR_CLASSES
            if self.status in {"failed", "lost"}
            else self.error_class is None
        )
        if self.status == "lost":
            require_completion_fact(self.error_class is ErrorClass.INFRASTRUCTURE)


@dataclass(frozen=True)
class RunTerminalDecision:
    status: Literal["succeeded", "failed"]

    def __post_init__(self) -> None:
        require_literal(self.status, frozenset({"succeeded", "failed"}))


AttemptDecision: TypeAlias = (
    NoCompletion
    | AwaitCompletionPrerequisite
    | ExpiryTransitionsRequired
    | AttemptTerminalDecision
)
RunDecision: TypeAlias = (
    NoCompletion
    | AwaitCompletionPrerequisite
    | ExpiryTransitionsRequired
    | RunTerminalDecision
)
