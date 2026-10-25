from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from kora.observability.policy import CONTEXT_IDENTIFIER

ScopeKind = Literal["run", "subgraph", "attempt"]

SCOPE_KINDS: frozenset[str] = frozenset({"run", "subgraph", "attempt"})
DEADLINE_GRACE: timedelta = timedelta(seconds=30)

NANOSECONDS_PER_SECOND: int = 10**9
SECONDS_PER_DAY: int = 86400
NANOSECONDS_PER_MICROSECOND: int = 1000


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def is_duration(value: object, minimum: timedelta, inclusive: bool) -> bool:
    if not isinstance(value, timedelta):
        return False
    return value >= minimum if inclusive else value > minimum


def is_utc(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() == timedelta(0)


def is_scope_kind(value: object) -> bool:
    return isinstance(value, str) and value in SCOPE_KINDS


def is_scope_identifier(value: object) -> bool:
    return isinstance(value, str) and CONTEXT_IDENTIFIER.match(value) is not None


@dataclass(frozen=True)
class ExecutionDeadline:
    at: datetime
    scope_kind: ScopeKind
    scope_id: str
    clock_uncertainty: timedelta
    grace: timedelta

    def __post_init__(self) -> None:
        require(is_utc(self.at), "at is a UTC datetime")
        require(
            is_scope_kind(self.scope_kind), "scope_kind is run, subgraph or attempt"
        )
        require(is_scope_identifier(self.scope_id), "scope_id is a bounded identifier")
        require(
            is_duration(self.clock_uncertainty, timedelta(0), inclusive=True),
            "clock_uncertainty is a non-negative duration",
        )
        require(self.grace == DEADLINE_GRACE, "grace is 30 seconds")


def duration_ns(duration: timedelta) -> int:
    seconds = duration.days * SECONDS_PER_DAY + duration.seconds
    return (
        seconds * NANOSECONDS_PER_SECOND
        + duration.microseconds * NANOSECONDS_PER_MICROSECOND
    )


def is_instant_ns(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class ClockSample:
    wall: datetime
    monotonic_ns: int

    def __post_init__(self) -> None:
        require(is_utc(self.wall), "wall is a UTC datetime")
        require(is_instant_ns(self.monotonic_ns), "monotonic_ns is non-negative")


@dataclass(frozen=True)
class LocalThresholds:
    interrupt_at_ns: int
    escalate_at_ns: int

    def __post_init__(self) -> None:
        require(
            self.interrupt_at_ns <= self.escalate_at_ns,
            "interruption comes no later than escalation",
        )


def thresholds(deadline: ExecutionDeadline, sample: ClockSample) -> LocalThresholds:
    remaining = duration_ns(deadline.at - sample.wall) - duration_ns(
        deadline.clock_uncertainty
    )
    grace = duration_ns(deadline.grace)
    return LocalThresholds(
        interrupt_at_ns=sample.monotonic_ns + max(0, remaining),
        escalate_at_ns=sample.monotonic_ns + max(0, remaining + grace),
    )


def cancellation_schedule(now_ns: int, grace: timedelta) -> LocalThresholds:
    grace_ns = duration_ns(grace)
    require(grace_ns > 0, "a cancellation grace is positive")
    return LocalThresholds(interrupt_at_ns=now_ns, escalate_at_ns=now_ns + grace_ns)


def tighten(
    installed: LocalThresholds | None, candidate: LocalThresholds
) -> LocalThresholds:
    if installed is None:
        return candidate
    return LocalThresholds(
        interrupt_at_ns=min(installed.interrupt_at_ns, candidate.interrupt_at_ns),
        escalate_at_ns=min(installed.escalate_at_ns, candidate.escalate_at_ns),
    )


def remaining_ns(threshold_ns: int, now_ns: int) -> int:
    return max(0, threshold_ns - now_ns)


def useful_phase_bound_ns(
    own_bound_ns: int, installed: LocalThresholds | None, now_ns: int
) -> int:
    require(own_bound_ns > 0, "a useful phase has a finite positive bound")
    if installed is None:
        return own_bound_ns
    return min(own_bound_ns, remaining_ns(installed.interrupt_at_ns, now_ns))


def interruption_due(installed: LocalThresholds | None, now_ns: int) -> bool:
    return installed is not None and now_ns >= installed.interrupt_at_ns


def escalation_due(installed: LocalThresholds | None, now_ns: int) -> bool:
    return installed is not None and now_ns >= installed.escalate_at_ns
