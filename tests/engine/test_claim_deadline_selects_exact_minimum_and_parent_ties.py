from datetime import UTC, datetime, timedelta

import pytest

from kora.engine.claim_timing import first_claim_deadline, prepare_claim_timeout
from kora.engine.deadline_records import (
    prepare_deadline_record,
)

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
