from sqlalchemy import Boolean, Column

from kora.db.types import collated_text

OPEN_ROWS: str = "current_dispatcher_open IS TRUE"
UNKNOWN_ROWS: str = "current_dispatcher_open IS NULL"
CLAIM_INDEXES: tuple[tuple[str, str, tuple[str, ...], str | None, bool], ...] = (
    (
        "ix_dispatcher_claim_receipt",
        "attempt_dispatcher",
        ("worker_id", "incarnation_id", "claim_id"),
        None,
        True,
    ),
    (
        "ix_attempt_worker_open",
        "attempt",
        ("worker_id", "lease_until", "id"),
        OPEN_ROWS,
        False,
    ),
    (
        "ix_attempt_worker_unknown",
        "attempt",
        ("worker_id", "lease_until", "id"),
        UNKNOWN_ROWS,
        False,
    ),
)
ClaimColumn = Column[str] | Column[int] | Column[bool]


def claim_columns() -> tuple[tuple[str, ClaimColumn], ...]:
    return (
        ("attempt", Column("current_dispatcher_open", Boolean, nullable=True)),
        ("attempt_dispatcher", Column("claim_id", collated_text(256), nullable=True)),
        (
            "attempt_dispatcher",
            Column("claim_digest", collated_text(64), nullable=True),
        ),
    )
