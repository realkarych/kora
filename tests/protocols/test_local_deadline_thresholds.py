from datetime import UTC, datetime, timedelta, timezone

import pytest

from kora.protocols.deadlines import (
    ClockSample,
    ExecutionDeadline,
    LocalThresholds,
    cancellation_schedule,
    duration_ns,
    escalation_due,
    interruption_due,
    remaining_ns,
    thresholds,
    tighten,
    useful_phase_bound_ns,
)

SECOND: int = 10**9
GRACE: timedelta = timedelta(seconds=30)
DEADLINE_AT: datetime = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
MONOTONIC: int = 1_000 * SECOND


def deadline(
    at: datetime = DEADLINE_AT, uncertainty: timedelta = timedelta(0)
) -> ExecutionDeadline:
    return ExecutionDeadline(at, "run", "run-1", uncertainty, GRACE)


def sample(
    seconds_before_deadline: float, monotonic_ns: int = MONOTONIC
) -> ClockSample:
    wall = DEADLINE_AT - timedelta(seconds=seconds_before_deadline)
    return ClockSample(wall, monotonic_ns)


FORMULA_CASES: list[tuple[float, timedelta, int, int]] = [
    (100, timedelta(0), 100 * SECOND, 130 * SECOND),
    (100, timedelta(seconds=7), 93 * SECOND, 123 * SECOND),
    (0, timedelta(0), 0, 30 * SECOND),
    (10, timedelta(0), 10 * SECOND, 40 * SECOND),
    (-10, timedelta(0), 0, 20 * SECOND),
    (-40, timedelta(0), 0, 0),
    (5, timedelta(seconds=10), 0, 25 * SECOND),
    (0.000001, timedelta(0), 1_000, 30 * SECOND + 1_000),
]
TIGHTEN_CASES: list[tuple[LocalThresholds, LocalThresholds, LocalThresholds]] = [
    (LocalThresholds(10, 40), LocalThresholds(20, 50), LocalThresholds(10, 40)),
    (LocalThresholds(20, 50), LocalThresholds(10, 40), LocalThresholds(10, 40)),
    (LocalThresholds(10, 60), LocalThresholds(20, 50), LocalThresholds(10, 50)),
    (LocalThresholds(10, 40), LocalThresholds(10, 40), LocalThresholds(10, 40)),
]
INVALID_SAMPLES: list[tuple[datetime, int]] = [
    (datetime(2026, 10, 21, 12, 0), MONOTONIC),
    (datetime(2026, 10, 21, 12, 0, tzinfo=timezone(timedelta(hours=1))), MONOTONIC),
    (DEADLINE_AT, -1),
]


@pytest.mark.parametrize(
    ("before", "uncertainty", "interrupt_offset", "escalate_offset"), FORMULA_CASES
)
def test_local_deadline_thresholds(
    before: float, uncertainty: timedelta, interrupt_offset: int, escalate_offset: int
) -> None:
    clock_sample = sample(before)

    installed = thresholds(deadline(uncertainty=uncertainty), clock_sample)

    assert installed == LocalThresholds(
        MONOTONIC + interrupt_offset, MONOTONIC + escalate_offset
    )


def test_local_deadline_thresholds_are_due_at_exact_equality() -> None:
    installed = thresholds(deadline(), sample(10))
    before = installed.interrupt_at_ns - 1

    at_interrupt = installed.interrupt_at_ns
    at_escalate = installed.escalate_at_ns

    assert not interruption_due(installed, before)
    assert interruption_due(installed, at_interrupt)
    assert not escalation_due(installed, at_escalate - 1)
    assert escalation_due(installed, at_escalate)


@pytest.mark.parametrize(("installed", "candidate", "expected"), TIGHTEN_CASES)
def test_local_deadline_thresholds_only_tighten(
    installed: LocalThresholds, candidate: LocalThresholds, expected: LocalThresholds
) -> None:
    current = installed

    tightened = tighten(current, candidate)

    assert tightened == expected
    assert tightened.interrupt_at_ns <= installed.interrupt_at_ns
    assert tightened.escalate_at_ns <= installed.escalate_at_ns


def test_local_deadline_thresholds_follow_wall_clock_jumps() -> None:
    installed = thresholds(deadline(), sample(100, MONOTONIC))
    forward = sample(50, MONOTONIC + 10 * SECOND)
    backward = sample(200, MONOTONIC + 10 * SECOND)

    after_forward = tighten(installed, thresholds(deadline(), forward))
    after_backward = tighten(installed, thresholds(deadline(), backward))

    assert after_forward == LocalThresholds(
        MONOTONIC + 60 * SECOND, MONOTONIC + 90 * SECOND
    )
    assert after_backward == installed


def test_local_deadline_cancellation_composes_through_tighten() -> None:
    installed = thresholds(deadline(), sample(100))
    early = MONOTONIC + 5 * SECOND
    late = MONOTONIC + 200 * SECOND

    before_deadline = tighten(installed, cancellation_schedule(early, GRACE))
    after_deadline = tighten(installed, cancellation_schedule(late, GRACE))
    unbound = tighten(None, cancellation_schedule(early, timedelta(seconds=5)))

    assert before_deadline == LocalThresholds(early, early + 30 * SECOND)
    assert after_deadline == installed
    assert unbound == LocalThresholds(early, early + 5 * SECOND)
    with pytest.raises(ValueError, match="grace"):
        cancellation_schedule(early, timedelta(0))


def test_local_deadline_useful_phase_bound_clips_and_floors() -> None:
    installed = thresholds(deadline(), sample(10))
    now = MONOTONIC

    clipped = useful_phase_bound_ns(60 * SECOND, installed, now)
    unclipped = useful_phase_bound_ns(5 * SECOND, installed, now)
    expired = useful_phase_bound_ns(60 * SECOND, installed, now + 20 * SECOND)
    unbound = useful_phase_bound_ns(60 * SECOND, None, now)

    assert clipped == 10 * SECOND
    assert unclipped == 5 * SECOND
    assert expired == 0
    assert unbound == 60 * SECOND
    assert remaining_ns(installed.interrupt_at_ns, now + 20 * SECOND) == 0
    with pytest.raises(ValueError, match="bound"):
        useful_phase_bound_ns(0, installed, now)


def test_local_deadline_null_binding_is_never_due() -> None:
    now = MONOTONIC

    interrupt = interruption_due(None, now)
    escalate = escalation_due(None, now)

    assert not interrupt
    assert not escalate


def test_local_deadline_extremes_raise_nothing() -> None:
    far = deadline(at=datetime.max.replace(tzinfo=UTC), uncertainty=timedelta.max)
    latest = deadline(at=datetime.max.replace(tzinfo=UTC))
    earliest = ClockSample(datetime.min.replace(tzinfo=UTC), 0)

    uncertain = thresholds(far, earliest)
    distant = thresholds(latest, earliest)

    assert uncertain == LocalThresholds(0, 0)
    assert distant.escalate_at_ns - distant.interrupt_at_ns == 30 * SECOND
    assert duration_ns(timedelta.max) > 0


@pytest.mark.parametrize(("wall", "monotonic_ns"), INVALID_SAMPLES)
def test_local_deadline_rejects_invalid_samples(
    wall: datetime, monotonic_ns: int
) -> None:
    with pytest.raises(ValueError, match="wall|monotonic"):
        ClockSample(wall, monotonic_ns)
