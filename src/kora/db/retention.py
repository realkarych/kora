from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias, cast

from pydantic import MISSING, TypeAdapter, ValidationError

from kora.db.types import JsonObject
from kora.ir.runtime import Retention
from kora.ir.scalars import RetentionPeriod

RETENTION_PERIOD: TypeAdapter[str] = TypeAdapter(RetentionPeriod)
INVALID_RETENTION: str = "retention snapshot is invalid"


class RetentionSnapshotError(ValueError):
    pass


def canonical_period(value: object) -> str:
    try:
        period = RETENTION_PERIOD.validate_python(value)
    except ValidationError:
        raise RetentionSnapshotError(INVALID_RETENTION) from None
    if period == "forever":
        return period
    days = period[:-1].lstrip("0")
    if days == "":
        days = "0"
    return f"{days}d"


@dataclass(frozen=True)
class RetentionSnapshot:
    transcripts: str
    artifact_payloads: str

    def __post_init__(self) -> None:
        for period in (self.transcripts, self.artifact_payloads):
            if canonical_period(period) != period:
                raise RetentionSnapshotError(INVALID_RETENTION)

    def to_json(self) -> JsonObject:
        return {
            "transcripts": self.transcripts,
            "artifact_payloads": self.artifact_payloads,
        }


@dataclass(frozen=True)
class UnversionedRetention:
    status: Literal["unversioned"] = "unversioned"


StoredRetention: TypeAlias = RetentionSnapshot | UnversionedRetention


def retention_field(
    source: object, field: Literal["transcripts", "artifact_payloads"]
) -> object:
    if source is MISSING:
        return MISSING
    if isinstance(source, Retention):
        return (
            source.transcripts if field == "transcripts" else source.artifact_payloads
        )
    if not isinstance(source, Mapping):
        raise RetentionSnapshotError(INVALID_RETENTION)
    values = cast("Mapping[object, object]", source)
    if field not in values:
        return MISSING
    selected = values[field]
    if selected is MISSING:
        raise RetentionSnapshotError(INVALID_RETENTION)
    return selected


def resolved_field(
    graph: object, namespace: object, field: Literal["transcripts", "artifact_payloads"]
) -> str:
    selected = retention_field(graph, field)
    if selected is MISSING:
        selected = retention_field(namespace, field)
    if selected is MISSING:
        selected = "30d"
    return canonical_period(selected)


def resolve_retention_snapshot(
    graph: object = MISSING, namespace: object = MISSING
) -> RetentionSnapshot:
    return RetentionSnapshot(
        transcripts=resolved_field(graph, namespace, "transcripts"),
        artifact_payloads=resolved_field(graph, namespace, "artifact_payloads"),
    )


def classify_retention_snapshot(version: object, value: object) -> StoredRetention:
    if version is None:
        return UnversionedRetention()
    if type(version) is not int or version != 1 or not isinstance(value, Mapping):
        raise RetentionSnapshotError(INVALID_RETENTION)
    values = cast("Mapping[object, object]", value)
    if (
        len(values) != 2
        or "transcripts" not in values
        or "artifact_payloads" not in values
    ):
        raise RetentionSnapshotError(INVALID_RETENTION)
    transcripts = canonical_period(values["transcripts"])
    artifact_payloads = canonical_period(values["artifact_payloads"])
    if (
        transcripts != values["transcripts"]
        or artifact_payloads != values["artifact_payloads"]
    ):
        raise RetentionSnapshotError(INVALID_RETENTION)
    return RetentionSnapshot(transcripts, artifact_payloads)
