from collections.abc import Sequence

from alembic import op

from kora.db.types import POSTGRESQL, SQLITE

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == POSTGRESQL:
        op.execute(
            'ALTER TABLE run ALTER COLUMN target_key TYPE VARCHAR(256) COLLATE "C"'
        )
    elif dialect != SQLITE:
        raise RuntimeError("unsupported run target key migration backend")


def downgrade() -> None:
    raise RuntimeError(
        "run target key migration cannot be downgraded; restore a compatible backup"
    )
