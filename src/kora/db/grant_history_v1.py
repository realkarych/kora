from sqlalchemy import BigInteger, Column, Index, Integer, text
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.types import TypeEngine

from kora.db.types import POSTGRESQL, JsonDocument, JsonObject, collated_text

VERIFIED_PREFIX: TextClause = text(
    "prefix_count IS NOT NULL AND prefix_digest IS NOT NULL"
)
OPEN_GRANT: TextClause = text("closed_at IS NULL")
PREFIX_INDEX: str = "ix_dispatcher_verified_prefix"
OPEN_INDEX: str = "ix_dispatcher_open_epoch"
TIP_FIELDS: tuple[str, ...] = (
    "grant_history_epoch",
    "grant_history_count",
    "grant_history_digest",
)
ACK_FIELDS: tuple[str, ...] = (
    "verified_history_epoch",
    "verified_history_count",
    "verified_history_digest",
)
HistoryColumn = Column[str] | Column[int] | Column[JsonObject]


def lease_epoch_type() -> TypeEngine[int]:
    return Integer().with_variant(BigInteger(), POSTGRESQL)


def dispatcher_history_columns() -> tuple[HistoryColumn, ...]:
    return (
        Column("process", JsonDocument(), nullable=True),
        Column("prefix_count", BigInteger, nullable=True),
        Column("prefix_digest", collated_text(64), nullable=True),
    )


def attempt_history_columns() -> tuple[HistoryColumn, ...]:
    return tuple(
        Column(name, collated_text(64), nullable=True)
        if name.endswith("digest")
        else Column(name, BigInteger(), nullable=True)
        for name in (*TIP_FIELDS, *ACK_FIELDS)
    )


def dispatcher_history_indexes() -> tuple[Index, ...]:
    return tuple(
        Index(
            name,
            "attempt_id",
            "lease_epoch",
            postgresql_where=predicate,
            sqlite_where=predicate,
        )
        for name, predicate in (
            (PREFIX_INDEX, VERIFIED_PREFIX),
            (OPEN_INDEX, OPEN_GRANT),
        )
    )
