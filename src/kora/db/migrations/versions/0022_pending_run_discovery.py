from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from kora.db.promotion_admission_v1 import TABLE_NAME, promotion_admission_elements
from kora.db.types import POSTGRESQL

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _create_index() -> None:
    connection = op.get_bind()
    if connection.dialect.name != POSTGRESQL:
        op.create_index("ix_run_status_id", "run", ["status", "id"])
        return
    with connection.execute(
        text(
            "SELECT indisvalid, indrelid = 'run'::regclass, indnkeyatts, "
            "pg_get_indexdef(indexrelid, 1, true), "
            "pg_get_indexdef(indexrelid, 2, true), "
            "indpred IS NULL FROM pg_index "
            "WHERE indexrelid = to_regclass('ix_run_status_id')"
        )
    ) as result:
        state = result.one_or_none()
    if state is not None and tuple(state[1:]) != (True, 2, "status", "id", True):
        raise RuntimeError("pending discovery index conflicts with its migration")
    valid = None if state is None else state[0]
    if valid is True:
        return
    with op.get_context().autocommit_block():
        if valid is False:
            op.drop_index(
                "ix_run_status_id", table_name="run", postgresql_concurrently=True
            )
        op.create_index(
            "ix_run_status_id", "run", ["status", "id"], postgresql_concurrently=True
        )


def upgrade() -> None:
    _create_index()
    op.create_table(TABLE_NAME, *promotion_admission_elements())


def downgrade() -> None:
    raise RuntimeError(
        "pending run discovery cannot be downgraded; restore a compatible backup"
    )
