from dataclasses import dataclass
from datetime import datetime, timedelta

from kora.protocols.runtime import ExecutionDeadline, is_utc, require

NANOSECONDS_PER_SECOND: int = 10**9
SECONDS_PER_DAY: int = 86400
NANOSECONDS_PER_MICROSECOND: int = 1000


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
