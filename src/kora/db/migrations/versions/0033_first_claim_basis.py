from collections.abc import Sequence

from alembic import op

from kora.db.first_claim_basis_v1 import TABLE_NAME, first_claim_basis_elements

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(TABLE_NAME, *first_claim_basis_elements(op.get_bind().dialect.name))


def downgrade() -> None:
    raise RuntimeError(
        "first-Claim basis cannot be downgraded; restore a compatible backup"
    )
