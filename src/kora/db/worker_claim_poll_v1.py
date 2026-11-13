from sqlalchemy import BigInteger, Column, text
from sqlalchemy.sql.elements import TextClause

from kora.db.types import collated_text

CANDIDATE_INDEX: str = "ix_attempt_claim_candidate"
CANDIDATE_PREDICATE: TextClause = text(
    "((status IN ('created', 'claimed', 'submitted', 'running', 'finishing') "
    "OR recovery_required IS TRUE) IS TRUE)"
)
CANDIDATE_ROWS: str = CANDIDATE_PREDICATE.text


def cursor_columns() -> tuple[Column[str] | Column[int], ...]:
    return (
        Column("claim_cursor", collated_text(26), nullable=True),
        Column(
            "claim_cursor_generation", BigInteger, nullable=True, server_default="0"
        ),
    )
