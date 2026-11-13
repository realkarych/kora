from datetime import UTC, datetime, timedelta

import pytest

from kora.engine.claim_timing import first_claim_deadline, prepare_claim_timeout
from kora.engine.deadline_records import (
    DeadlineRecordRejected,
    deadline_document,
    prepare_deadline_record,
)
from kora.protocols.deadlines import ClockSample, thresholds

START: datetime = datetime(2026, 11, 13, 16, 0, 0, 123456, tzinfo=UTC)


@pytest.mark.parametrize(
    ("run_timeout", "attempt_timeout", "scope", "seconds"),
    [
        ("2m", "30s", "attempt", 30),
        ("60s", "30s", "run", 30),
        (None, "0ms", "attempt", 0),
        ("9" * 5000 + "d", "1ms", "attempt", 0.001),
        ("1h", "9" * 5000 + "d", "run", 3570),
        ("0000001h", "00000001m", "attempt", 60),
    ],
)
def test_claim_deadline_selects_exact_minimum_and_parent_ties(
    run_timeout: str | None,
    attempt_timeout: str,
    scope: str,
    seconds: float,
) -> None:
    parent = prepare_claim_timeout(run_timeout)
    own = prepare_claim_timeout(attempt_timeout)
    now = START + timedelta(seconds=30)
    uncertainty = timedelta(microseconds=17)

    result = first_claim_deadline(
        "run", "attempt", START, now, parent, own, uncertainty
    )

    assert result is not None
    assert result.at == now + timedelta(seconds=seconds)
    assert result.scope_kind == result.scope_id == scope
    assert result.clock_uncertainty == uncertainty
    assert prepare_deadline_record(result).value is not None


def test_claim_deadline_preserves_explicit_unlimited() -> None:
    parent = prepare_claim_timeout(None)
    own = prepare_claim_timeout(None)

    result = first_claim_deadline(
        "run", "attempt", START, START, parent, own, timedelta(0)
    )

    assert deadline_document(prepare_deadline_record(result)) == {
        "version": 1,
        "value": None,
    }


@pytest.mark.parametrize("own", [None, "9" * 5000 + "d"])
def test_claim_deadline_refuses_unrepresentable_minimum(own: str | None) -> None:
    parent = prepare_claim_timeout("9" * 5000 + "d")
    child = prepare_claim_timeout(own)

    with pytest.raises(DeadlineRecordRejected):
        first_claim_deadline(
            "run", "attempt", START, START, parent, child, timedelta(0)
        )


def test_claim_deadline_does_not_require_representable_grace_timestamp() -> None:
    now = datetime.max.replace(tzinfo=UTC) - timedelta(seconds=1)
    own = prepare_claim_timeout("1s")

    result = first_claim_deadline("run", "attempt", now, now, None, own, timedelta(0))

    assert result is not None
    assert result.at == datetime.max.replace(tzinfo=UTC)
    timers = thresholds(result, ClockSample(now, 0))
    assert timers.interrupt_at_ns == 1_000_000_000
    assert timers.escalate_at_ns == 31_000_000_000


def test_claim_deadline_checks_sum_before_datetime_conversion() -> None:
    now = datetime.max.replace(tzinfo=UTC) - timedelta(microseconds=1)
    duration = prepare_claim_timeout("1ms")

    with pytest.raises(DeadlineRecordRejected):
        first_claim_deadline("run", "attempt", now, now, None, duration, timedelta(0))


@pytest.mark.parametrize("value", ["-1s", "1.5s", "1us", "1S", "", "1s\n"])
def test_claim_timeout_refuses_invalid_units_and_spelling(value: str) -> None:
    with pytest.raises(DeadlineRecordRejected):
        prepare_claim_timeout(value)
