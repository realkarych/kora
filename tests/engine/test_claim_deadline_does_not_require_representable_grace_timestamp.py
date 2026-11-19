from datetime import UTC, datetime, timedelta

from kora.engine.claim_timing import first_claim_deadline, prepare_claim_timeout
from kora.protocols.deadlines import ClockSample, thresholds

START: datetime = datetime(2026, 11, 13, 16, 0, 0, 123456, tzinfo=UTC)


def test_claim_deadline_does_not_require_representable_grace_timestamp() -> None:
    now = datetime.max.replace(tzinfo=UTC) - timedelta(seconds=1)
    own = prepare_claim_timeout("1s")

    result = first_claim_deadline("run", "attempt", now, now, None, own, timedelta(0))

    assert result is not None
    assert result.at == datetime.max.replace(tzinfo=UTC)
    timers = thresholds(result, ClockSample(now, 0))
    assert timers.interrupt_at_ns == 1_000_000_000
    assert timers.escalate_at_ns == 31_000_000_000
