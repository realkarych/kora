from datetime import UTC, datetime, timedelta

import pytest

from kora.engine.claim_timing import first_claim_deadline, prepare_claim_timeout
from kora.engine.deadline_records import (
    DeadlineRecordRejected,
)

START: datetime = datetime(2026, 11, 13, 16, 0, 0, 123456, tzinfo=UTC)


def test_claim_deadline_checks_sum_before_datetime_conversion() -> None:
    now = datetime.max.replace(tzinfo=UTC) - timedelta(microseconds=1)
    duration = prepare_claim_timeout("1ms")
    earlier = prepare_claim_timeout("500ms")
    later = prepare_claim_timeout("1s")
    admitted_at = now - timedelta(seconds=2)

    with pytest.raises(DeadlineRecordRejected):
        first_claim_deadline("run", "attempt", now, now, None, duration, timedelta(0))
    with pytest.raises(DeadlineRecordRejected) as unrepresentable_run:
        first_claim_deadline(
            "run",
            "attempt",
            now,
            now,
            earlier,
            later,
            timedelta(0),
            admitted_at=admitted_at,
        )

    assert type(unrepresentable_run.value) is DeadlineRecordRejected
