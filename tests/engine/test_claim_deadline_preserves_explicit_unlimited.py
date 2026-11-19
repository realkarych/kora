from datetime import UTC, datetime, timedelta

from kora.engine.claim_timing import first_claim_deadline, prepare_claim_timeout
from kora.engine.deadline_records import (
    deadline_document,
    prepare_deadline_record,
)

START: datetime = datetime(2026, 11, 13, 16, 0, 0, 123456, tzinfo=UTC)


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
