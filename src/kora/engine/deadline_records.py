from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeAlias

from kora.db.types import JsonValue
from kora.ir.json import parse_json_object
from kora.protocols.deadline_records import (
    MAX_DEADLINE_RECORD_BYTES as MAX_DEADLINE_RECORD_BYTES,
)
from kora.protocols.deadline_records import (
    MAX_UNCERTAINTY_US as MAX_UNCERTAINTY_US,
)
from kora.protocols.deadline_records import (
    MICROSECONDS_PER_SECOND as MICROSECONDS_PER_SECOND,
)
from kora.protocols.deadline_records import (
    SECONDS_PER_DAY as SECONDS_PER_DAY,
)
from kora.protocols.deadline_records import (
    CanonicalInstant as CanonicalInstant,
)
from kora.protocols.deadline_records import (
    DeadlineEnvelope as DeadlineEnvelope,
)
from kora.protocols.deadline_records import (
    DeadlineRecordRejected as DeadlineRecordRejected,
)
from kora.protocols.deadline_records import (
    DeadlineValue as DeadlineValue,
)
from kora.protocols.deadline_records import (
    Microseconds as Microseconds,
)
from kora.protocols.deadline_records import (
    duration_us as duration_us,
)
from kora.protocols.deadline_records import (
    instant_text as instant_text,
)
from kora.protocols.deadline_records import (
    parsed_instant as parsed_instant,
)
from kora.protocols.deadline_records import (
    refuse as refuse,
)
from kora.protocols.deadlines import ExecutionDeadline, is_utc


@dataclass(frozen=True)
class UnrecordedDeadlineBinding:
    legacy_at: datetime | None


@dataclass(frozen=True)
class RecordedDeadlineBinding:
    deadline: ExecutionDeadline | None


DecodedDeadline: TypeAlias = UnrecordedDeadlineBinding | RecordedDeadlineBinding


def prepare_deadline_record(deadline: ExecutionDeadline | None) -> DeadlineEnvelope:
    try:
        value: DeadlineValue | None = None
        if deadline is not None:
            if deadline.grace != timedelta(seconds=30):
                refuse()
            value = DeadlineValue(
                at=instant_text(deadline.at),
                scope_kind=deadline.scope_kind,
                scope_id=deadline.scope_id,
                clock_uncertainty_us=str(duration_us(deadline.clock_uncertainty)),
                grace_seconds=30,
            )
        return DeadlineEnvelope(version=1, value=value)
    except (ValueError, TypeError, OverflowError):
        raise DeadlineRecordRejected() from None


def deadline_document(envelope: DeadlineEnvelope) -> dict[str, JsonValue]:
    try:
        return parse_json_object(
            envelope.model_dump(mode="json"),
            max_depth=2,
            max_bytes=MAX_DEADLINE_RECORD_BYTES,
        )
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise DeadlineRecordRejected() from None


def decode_deadline_record(
    value: JsonValue | None, legacy_at: datetime | None
) -> DecodedDeadline:
    try:
        if legacy_at is not None and not is_utc(legacy_at):
            refuse()
        if value is None:
            return UnrecordedDeadlineBinding(legacy_at)
        document = parse_json_object(
            value, max_depth=2, max_bytes=MAX_DEADLINE_RECORD_BYTES
        )
        envelope = DeadlineEnvelope.model_validate(document)
        deadline = None if envelope.value is None else envelope.value.deadline()
        projection = None if deadline is None else deadline.at
        if projection != legacy_at:
            refuse()
        return RecordedDeadlineBinding(deadline)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise DeadlineRecordRejected() from None
