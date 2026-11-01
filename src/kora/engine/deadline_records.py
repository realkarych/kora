from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Never, Self, TypeAlias

from pydantic import Field, StrictInt, StringConstraints, model_validator

from kora.db.types import JsonValue
from kora.ir.json import parse_json_object
from kora.protocols.boundary import BoundaryModel
from kora.protocols.deadlines import ExecutionDeadline, ScopeKind, is_utc
from kora.protocols.identity import Identifier

MICROSECONDS_PER_SECOND: int = 1_000_000
SECONDS_PER_DAY: int = 86_400
MAX_DEADLINE_RECORD_BYTES: int = 4096


def duration_us(value: timedelta) -> int:
    return (
        value.days * SECONDS_PER_DAY + value.seconds
    ) * MICROSECONDS_PER_SECOND + value.microseconds


MAX_UNCERTAINTY_US: int = duration_us(timedelta.max)
CanonicalInstant: TypeAlias = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=27,
        max_length=27,
        pattern=r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\z",
    ),
]
Microseconds: TypeAlias = Annotated[
    str,
    StringConstraints(strict=True, max_length=20, pattern=r"\A(?:0|[1-9][0-9]*)\z"),
]


class DeadlineRecordRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("the saved deadline binding is invalid")


def refuse() -> Never:
    raise DeadlineRecordRejected()


def instant_text(value: datetime) -> str:
    if not is_utc(value):
        refuse()
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parsed_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if instant_text(parsed) != value:
        refuse()
    return parsed


class DeadlineValue(BoundaryModel):
    at: CanonicalInstant
    scope_kind: ScopeKind
    scope_id: Identifier
    clock_uncertainty_us: Microseconds
    grace_seconds: Annotated[StrictInt, Field(ge=30, le=30)]

    @model_validator(mode="after")
    def exact_domain(self) -> Self:
        parsed_instant(self.at)
        if int(self.clock_uncertainty_us) > MAX_UNCERTAINTY_US:
            refuse()
        return self

    def deadline(self) -> ExecutionDeadline:
        return ExecutionDeadline(
            at=parsed_instant(self.at),
            scope_kind=self.scope_kind,
            scope_id=self.scope_id,
            clock_uncertainty=timedelta(microseconds=int(self.clock_uncertainty_us)),
            grace=timedelta(seconds=self.grace_seconds),
        )


class DeadlineEnvelope(BoundaryModel):
    version: Annotated[StrictInt, Field(ge=1, le=1)]
    value: DeadlineValue | None


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
