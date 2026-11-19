from datetime import UTC, datetime, timedelta

import pytest

from kora.engine.claim_timing import first_claim_deadline, prepare_claim_timeout
from kora.engine.deadline_records import (
    DeadlineRecordRejected,
)

START: datetime = datetime(2026, 11, 13, 16, 0, 0, 123456, tzinfo=UTC)


@pytest.mark.parametrize("own", [None, "9" * 5000 + "d"])
def test_claim_deadline_refuses_unrepresentable_minimum(own: str | None) -> None:
    parent = prepare_claim_timeout("9" * 5000 + "d")
    child = prepare_claim_timeout(own)

    with pytest.raises(DeadlineRecordRejected):
        first_claim_deadline(
            "run", "attempt", START, START, parent, child, timedelta(0)
        )
