from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.schema import Constraint, conv

from kora.db.types import CollatedText

TABLE_NAME = "run_promotion_admission"
SchemaElement = Column[str] | Column[int] | Constraint


def promotion_admission_elements() -> tuple[SchemaElement, ...]:
    return (
        Column("run_id", CollatedText(26), nullable=False),
        Column("receipt_id", CollatedText(26), nullable=False),
        Column("namespace_id", CollatedText(26), nullable=False),
        Column("graph_id", CollatedText(26), nullable=False),
        Column("graph_version_id", CollatedText(26), nullable=False),
        Column("version", Integer(), nullable=False),
        Column("run_bytes", BigInteger(), nullable=False),
        Column("receipt_bytes", BigInteger(), nullable=False),
        PrimaryKeyConstraint("run_id", name="pk_run_promotion_admission"),
        UniqueConstraint("receipt_id", name="uq_run_promotion_admission_receipt"),
        ForeignKeyConstraint(
            ("run_id",), ("run.id",), name="fk_run_promotion_admission_run"
        ),
        ForeignKeyConstraint(
            ("receipt_id",),
            ("inbound_event.id",),
            name="fk_run_promotion_admission_receipt",
        ),
        CheckConstraint(
            "version = 1 AND run_bytes > 0 AND receipt_bytes > 0",
            name=conv("ck_run_promotion_admission_bounds"),
        ),
    )
