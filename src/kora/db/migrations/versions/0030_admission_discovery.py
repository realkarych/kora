from collections.abc import Sequence

from alembic import op
from sqlalchemy import Boolean, literal_column, text

from kora.db.admission_discovery_v1 import CANDIDATE_ROWS, INDEX_NAME
from kora.db.types import POSTGRESQL

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    predicate = literal_column(CANDIDATE_ROWS, Boolean())
    if op.get_bind().dialect.name != POSTGRESQL:
        op.create_index(INDEX_NAME, "node_instance", ["id"], sqlite_where=predicate)
        return
    with op.get_bind().execute(
        text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
        {"name": INDEX_NAME},
    ) as result:
        valid = result.scalar_one_or_none()
    if valid is True:
        return
    with op.get_context().autocommit_block():
        if valid is False:
            op.drop_index(
                INDEX_NAME, table_name="node_instance", postgresql_concurrently=True
            )
        op.create_index(
            INDEX_NAME,
            "node_instance",
            ["id"],
            postgresql_where=predicate,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    raise RuntimeError(
        "admission discovery cannot be downgraded; restore a compatible backup"
    )
