from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column, text

from kora.db.types import POSTGRESQL, UsdAmount

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == POSTGRESQL:
        op.execute(
            text(
                "ALTER TABLE usage_record ALTER COLUMN source "
                "TYPE TEXT USING source::text"
            )
        )
    op.add_column(
        "usage_record", Column("same_basis_usd_floor", UsdAmount(), nullable=True)
    )
    op.execute(
        text(
            "UPDATE usage_record SET same_basis_usd_floor = usd_estimate "
            "WHERE normalization_version = 1 AND usd_estimate IS NOT NULL"
        )
    )


def downgrade() -> None:
    raise RuntimeError("worker usage cannot be downgraded; restore a compatible backup")
