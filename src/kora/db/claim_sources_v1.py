from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Column, ForeignKey, Integer

from kora.db.run_input_locator_v1 import canonical_locator_key
from kora.db.types import UtcTimestamp, collated_text

TABLE_NAME: str = "attempt_claim_read_admission"
SOURCE_FIELDS: tuple[str, ...] = (
    "input_bindings",
    "execution_policy",
    "execution_request",
    "handle",
    "deadline_binding",
)
LOCATORS: tuple[tuple[str, str, str], ...] = (
    ("attempt", "id", "ix_claim_attempt_locator"),
    ("node_instance", "id", "ix_claim_node_locator"),
    (TABLE_NAME, "attempt_id", "ix_claim_admission_locator"),
)
SQLITE_KEY: str = canonical_locator_key("id")
SQLITE_ADMISSION_KEY: str = canonical_locator_key("attempt_id")
MAX_SOURCE_COUNT: int = 2**63 - 1
Element = Column[str] | Column[int] | Column[datetime] | CheckConstraint


def claim_source_elements() -> tuple[Element, ...]:
    return (
        Column(
            "attempt_id", collated_text(26), ForeignKey("attempt.id"), primary_key=True
        ),
        Column("format_version", Integer, nullable=False),
        Column("source_revision", BigInteger, nullable=False),
        CheckConstraint("format_version = 1", name="format_version"),
        CheckConstraint("source_revision > 0", name="source_revision"),
        *(
            Column(f"{field}_{kind}_bytes", BigInteger, nullable=False)
            for field in SOURCE_FIELDS
            for kind in ("native", "transport")
        ),
        *(
            CheckConstraint(
                f"({field}_native_bytes = 0 AND {field}_transport_bytes = 0) OR "
                f"({field}_native_bytes > 0 AND {field}_transport_bytes > 0)",
                name=f"{field}_presence",
            )
            for field in SOURCE_FIELDS
        ),
        Column("created_at", UtcTimestamp(), nullable=False),
        Column("updated_at", UtcTimestamp(), nullable=False),
    )
