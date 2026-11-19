from datetime import UTC, datetime

import pytest

from kora.engine.claim_timing import prepare_claim_timeout
from kora.engine.deadline_records import (
    DeadlineRecordRejected,
)

START: datetime = datetime(2026, 11, 13, 16, 0, 0, 123456, tzinfo=UTC)


@pytest.mark.parametrize("value", ["-1s", "1.5s", "1us", "1S", "", "1s\n"])
def test_claim_timeout_refuses_invalid_units_and_spelling(value: str) -> None:
    with pytest.raises(DeadlineRecordRejected):
        prepare_claim_timeout(value)
