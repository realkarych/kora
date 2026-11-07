from collections.abc import Sequence

from alembic import op

from kora.db.storage_address_fence_v1 import (
    TABLE_NAME,
    storage_address_fence_elements_v1,
)

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(
        TABLE_NAME,
        *storage_address_fence_elements_v1(dialect),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    raise RuntimeError(
        "storage address fence history cannot be downgraded; "
        "restore a compatible backup"
    )
