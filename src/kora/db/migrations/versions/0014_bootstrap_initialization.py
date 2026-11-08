from collections.abc import Sequence

from alembic import op

from kora.db.bootstrap_initialization_v1 import (
    TABLE_NAME,
    bootstrap_initialization_elements_v1,
)

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(TABLE_NAME, *bootstrap_initialization_elements_v1(dialect))


def downgrade() -> None:
    raise RuntimeError(
        "bootstrap initialization cannot be downgraded; restore a compatible backup"
    )
