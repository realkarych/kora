from collections.abc import Sequence

from alembic import op
from sqlalchemy import literal_column

from kora.db.graph_publication_ordinal_v1 import (
    ORDINAL_INVALID_COLUMNS,
    ORDINAL_INVALID_INDEX,
    ORDINAL_VALID_COLUMNS,
    ORDINAL_VALID_INDEX,
    SQLITE_ORDINAL_INVALID,
    SQLITE_ORDINAL_VALID,
)
from kora.db.types import POSTGRESQL, SQLITE

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == POSTGRESQL:
        return
    if dialect != SQLITE:
        raise RuntimeError("unsupported graph ordinal migration backend")
    op.create_index(
        ORDINAL_VALID_INDEX,
        "graph_version",
        ORDINAL_VALID_COLUMNS,
        sqlite_where=literal_column(SQLITE_ORDINAL_VALID),
    )
    op.create_index(
        ORDINAL_INVALID_INDEX,
        "graph_version",
        ORDINAL_INVALID_COLUMNS,
        sqlite_where=literal_column(SQLITE_ORDINAL_INVALID),
    )


def downgrade() -> None:
    raise RuntimeError(
        "graph ordinal migration cannot be downgraded; restore a compatible backup"
    )
