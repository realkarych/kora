from collections.abc import Sequence

from alembic import op
from sqlalchemy import BigInteger, Column, literal_column

from kora.db.claim_sources_v1 import (
    LOCATORS,
    SQLITE_ADMISSION_KEY,
    SQLITE_KEY,
    TABLE_NAME,
    claim_source_elements,
)
from kora.db.types import SQLITE

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "attempt", Column("acknowledged_segment_offset", BigInteger, nullable=True)
    )
    op.create_table(TABLE_NAME, *claim_source_elements())
    if op.get_bind().dialect.name == SQLITE:
        for table, key, index in LOCATORS:
            op.create_index(
                index,
                table,
                [literal_column(f"{key} COLLATE BINARY")],
                sqlite_where=literal_column(
                    SQLITE_KEY if key == "id" else SQLITE_ADMISSION_KEY
                ),
            )


def downgrade() -> None:
    raise RuntimeError(
        "claim sources cannot be downgraded; restore a compatible backup"
    )
