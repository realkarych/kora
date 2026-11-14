import re
from datetime import UTC, datetime, timedelta
from enum import Enum

from kora.engine.deadline_records import DeadlineRecordRejected, duration_us
from kora.engine.expiry_reads import UNITS
from kora.protocols.deadlines import ExecutionDeadline, ScopeKind, is_utc

EARLIEST: datetime = datetime.min.replace(tzinfo=UTC)
LATEST_US: int = duration_us(datetime.max.replace(tzinfo=UTC) - EARLIEST)


class BeyondTimestamp(Enum):
    VALUE = "beyond_timestamp"


ClaimTimeout = int | BeyondTimestamp | None


def prepare_claim_timeout(value: str | None) -> ClaimTimeout:
    if value is None:
        return None
    match = re.fullmatch(r"([0-9]+)(ms|s|m|h|d)", value)
    if match is None:
        raise DeadlineRecordRejected()
    digits, unit = match.groups()
    digits = digits.lstrip("0") or "0"
    maximum = str(LATEST_US // UNITS[unit])
    if (len(digits), digits) > (len(maximum), maximum):
        return BeyondTimestamp.VALUE
    return int(digits) * UNITS[unit]


def first_claim_deadline(
    run_id: str,
    attempt_id: str,
    run_started_at: datetime,
    now: datetime,
    run_timeout: ClaimTimeout,
    attempt_timeout: ClaimTimeout,
    uncertainty: timedelta,
) -> ExecutionDeadline | None:
    if not is_utc(run_started_at) or not is_utc(now) or uncertainty < timedelta(0):
        raise DeadlineRecordRejected()
    candidates: list[tuple[int, ScopeKind, str]] = []
    scopes: tuple[tuple[datetime, ClaimTimeout, ScopeKind, str], ...] = (
        (run_started_at, run_timeout, "run", run_id),
        (now, attempt_timeout, "attempt", attempt_id),
    )
    for start, duration, kind, identity in scopes:
        if type(duration) is int:
            if duration < 0:
                raise DeadlineRecordRejected()
            candidates.append(
                (duration_us(start - EARLIEST) + duration, kind, identity)
            )
    if not candidates:
        if run_timeout is not None or attempt_timeout is not None:
            raise DeadlineRecordRejected()
        return None
    at, kind, identity = min(candidates, key=lambda entry: entry[0])
    if at > LATEST_US:
        raise DeadlineRecordRejected()
    return ExecutionDeadline(
        at=EARLIEST + timedelta(microseconds=at),
        scope_kind=kind,
        scope_id=identity,
        clock_uncertainty=uncertainty,
        grace=timedelta(seconds=30),
    )
