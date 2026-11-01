from dataclasses import dataclass
from typing import Literal, NoReturn, TypeAlias

from kora.cel_eval.errors import ErrorKind
from kora.ir.node import ErrorClass

RUNTIME_RETRY_CLASSES: frozenset[ErrorClass] = frozenset(
    (ErrorClass.INFRASTRUCTURE, ErrorClass.PROVIDER_LIMITED)
)


def _refuse() -> NoReturn:
    raise ValueError("retry facts are invalid")


def _require_kind(value: object, expected: type[object]) -> None:
    if type(value) is not expected:
        _refuse()


def _require_membership(value: frozenset[ErrorClass]) -> None:
    _require_kind(value, frozenset)
    for item in value:
        _require_kind(item, ErrorClass)


@dataclass(frozen=True)
class NodeRetryPolicyFacts:
    max_attempts: int
    retry_on: frozenset[ErrorClass]
    non_retryable: frozenset[ErrorClass]
    predicate_configured: bool

    def __post_init__(self) -> None:
        _require_kind(self.max_attempts, int)
        _require_kind(self.predicate_configured, bool)
        _require_membership(self.retry_on)
        _require_membership(self.non_retryable)
        if not 1 <= self.max_attempts <= 100:
            _refuse()


@dataclass(frozen=True)
class NoPredicate:
    pass


@dataclass(frozen=True)
class PredicatePending:
    pass


@dataclass(frozen=True)
class PredicateResult:
    value: bool

    def __post_init__(self) -> None:
        _require_kind(self.value, bool)


@dataclass(frozen=True)
class PredicateFailure:
    kind: ErrorKind

    def __post_init__(self) -> None:
        _require_kind(self.kind, ErrorKind)


RetryPredicate: TypeAlias = (
    NoPredicate | PredicatePending | PredicateResult | PredicateFailure
)


def _require_predicate(value: object) -> None:
    kinds = (NoPredicate, PredicatePending, PredicateResult, PredicateFailure)
    if not any(type(value) is kind for kind in kinds):
        _refuse()


@dataclass(frozen=True)
class NodeRetryFacts:
    failure_class: ErrorClass
    policy: NodeRetryPolicyFacts
    consumed_policy_attempts: int
    predicate: RetryPredicate

    def __post_init__(self) -> None:
        _require_kind(self.failure_class, ErrorClass)
        _require_kind(self.policy, NodeRetryPolicyFacts)
        _require_kind(self.consumed_policy_attempts, int)
        _require_predicate(self.predicate)
        minimum = 0 if self.failure_class in RUNTIME_RETRY_CLASSES else 1
        if self.consumed_policy_attempts < minimum:
            _refuse()
        if self.policy.predicate_configured == isinstance(self.predicate, NoPredicate):
            _refuse()


RetryDenialReason: TypeAlias = Literal[
    "never_retryable",
    "non_retryable",
    "not_selected",
    "attempts_exhausted",
    "predicate_false",
    "predicate_error",
]


@dataclass(frozen=True)
class NodeRetryDenied:
    failure_class: ErrorClass
    reason: RetryDenialReason
    predicate_error_kind: ErrorKind | None = None


@dataclass(frozen=True)
class RetryPredicateRequired:
    failure_class: ErrorClass


@dataclass(frozen=True)
class NodeRetryPolicyAllows:
    failure_class: ErrorClass


@dataclass(frozen=True)
class RuntimeRetryPolicyRequired:
    failure_class: ErrorClass


NodeRetryDecision: TypeAlias = (
    NodeRetryDenied
    | RetryPredicateRequired
    | NodeRetryPolicyAllows
    | RuntimeRetryPolicyRequired
)
