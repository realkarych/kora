from datetime import UTC, datetime, timedelta

import pytest

from kora.db.types import JsonValue
from kora.engine.deadline_records import (
    DeadlineRecordRejected,
    RecordedDeadlineBinding,
    UnrecordedDeadlineBinding,
    deadline_document,
    decode_deadline_record,
    prepare_deadline_record,
)
from kora.protocols.deadlines import ClockSample, ExecutionDeadline, thresholds

AT: datetime = datetime(2026, 11, 1, 12, 0, 0, 123456, tzinfo=UTC)


def deadline(uncertainty: timedelta) -> ExecutionDeadline:
    return ExecutionDeadline(
        at=AT,
        scope_kind="subgraph",
        scope_id="concrete-wrapper-17",
        clock_uncertainty=uncertainty,
        grace=timedelta(seconds=30),
    )


@pytest.mark.parametrize("legacy", [None, AT])
def test_deadline_record_distinguishes_unrecorded_and_unlimited(
    legacy: datetime | None,
) -> None:
    envelope = prepare_deadline_record(None)

    unknown = decode_deadline_record(None, legacy)
    unlimited = decode_deadline_record(deadline_document(envelope), None)

    assert isinstance(unknown, UnrecordedDeadlineBinding)
    assert unknown.legacy_at == legacy
    assert isinstance(unlimited, RecordedDeadlineBinding)
    assert unlimited.deadline is None
    assert deadline_document(envelope) == {"version": 1, "value": None}


@pytest.mark.parametrize(
    "uncertainty",
    [
        timedelta(0),
        timedelta(microseconds=1),
        timedelta(days=3, seconds=7, microseconds=123),
        timedelta.max,
    ],
)
def test_deadline_record_preserves_exact_timing(uncertainty: timedelta) -> None:
    original = deadline(uncertainty)
    sample = ClockSample(AT - timedelta(seconds=5), 1000)

    envelope = prepare_deadline_record(original)
    decoded = decode_deadline_record(deadline_document(envelope), AT)

    assert isinstance(decoded, RecordedDeadlineBinding)
    assert decoded.deadline == original
    assert decoded.deadline is not None
    assert thresholds(decoded.deadline, sample) == thresholds(original, sample)
    assert envelope.value is not None
    assert envelope.value.at == "2026-11-01T12:00:00.123456Z"
    assert envelope.value.grace_seconds == 30


@pytest.mark.parametrize("scalar", [None, AT + timedelta(microseconds=1)])
def test_deadline_record_refuses_inconsistent_projection(
    scalar: datetime | None,
) -> None:
    record = deadline_document(prepare_deadline_record(deadline(timedelta(0))))

    with pytest.raises(DeadlineRecordRejected):
        decode_deadline_record(record, scalar)


def test_unlimited_binding_refuses_a_nonnull_projection() -> None:
    record = deadline_document(prepare_deadline_record(None))

    with pytest.raises(DeadlineRecordRejected):
        decode_deadline_record(record, AT)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("at", "2026-11-01T12:00:00.123456+00:00"),
        ("at", "2026-11-01T12:00:00Z"),
        ("at", "2026-02-30T12:00:00.123456Z"),
        ("at", "2026-11-01T12:00:00.123456+03:00"),
        ("clock_uncertainty_us", "-1"),
        ("clock_uncertainty_us", "00"),
        ("clock_uncertainty_us", "1.0"),
        ("clock_uncertainty_us", "863999999999999999999"),
        ("clock_uncertainty_us", "86400000000000000000"),
        ("clock_uncertainty_us", 1),
        ("clock_uncertainty_us", "１"),
        ("grace_seconds", 31),
        ("grace_seconds", "30"),
        ("scope_kind", "node"),
        ("scope_id", "not a concrete identifier"),
    ],
)
def test_deadline_record_refuses_malformed_recorded_values(
    field: str, value: JsonValue
) -> None:
    record = deadline_document(prepare_deadline_record(deadline(timedelta(0))))
    current = record["value"]
    assert isinstance(current, dict)
    changed: dict[str, JsonValue] = {"version": 1, "value": {**current, field: value}}

    with pytest.raises(
        DeadlineRecordRejected, match="^the saved deadline binding is invalid$"
    ):
        decode_deadline_record(changed, AT)


@pytest.mark.parametrize(
    "record",
    [
        True,
        [],
        "null",
        {},
        {"version": 1},
        {"version": True, "value": None},
        {"version": 2, "value": None},
    ],
)
def test_deadline_record_never_defaults_unknown_recorded_data(
    record: JsonValue,
) -> None:
    with pytest.raises(DeadlineRecordRejected):
        decode_deadline_record(record, None)


def test_deadline_record_preserves_full_uncertainty_decimal_string() -> None:
    record = prepare_deadline_record(deadline(timedelta.max))

    document = deadline_document(record)

    assert document["value"] == {
        "at": "2026-11-01T12:00:00.123456Z",
        "scope_kind": "subgraph",
        "scope_id": "concrete-wrapper-17",
        "clock_uncertainty_us": "86399999999999999999",
        "grace_seconds": 30,
    }
