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

    with pytest.raises(DeadlineRecordRejected):
        first_claim_deadline("run", "attempt", now, now, None, duration, timedelta(0))
