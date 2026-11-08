from collections.abc import Sequence

from alembic import op

from kora.db.graph_preparation_capacity_v1 import (
    TABLE_NAME,
    graph_preparation_capacity_elements_v1,
)

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(TABLE_NAME, *graph_preparation_capacity_elements_v1(dialect))


def downgrade() -> None:
    raise RuntimeError(
        "graph preparation capacity cannot be downgraded; restore a compatible backup"
    )
