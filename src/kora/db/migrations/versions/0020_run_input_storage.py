from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column, Integer

from kora.db.run_inputs_v1 import TABLE_NAME, run_input_elements_v1

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    elements = run_input_elements_v1(op.get_bind().dialect.name)
    op.add_column("run", Column("input_encoding_version", Integer(), nullable=True))
    op.create_table(TABLE_NAME, *elements)


def downgrade() -> None:
    raise RuntimeError(
        "run input history cannot be downgraded; restore a compatible backup"
    )
