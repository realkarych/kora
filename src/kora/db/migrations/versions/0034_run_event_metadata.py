from collections.abc import Sequence

from alembic import op

from kora.db.run_event_page_v1 import RUN_EVENT_REVISION, SQLITE_EVENT_DDL
from kora.db.types import POSTGRESQL, SQLITE

revision: str = RUN_EVENT_REVISION
down_revision: str | None = "0033"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == SQLITE:
        op.execute(SQLITE_EVENT_DDL)
    elif dialect != POSTGRESQL:
        raise RuntimeError("unsupported event metadata migration backend")


def downgrade() -> None:
    raise RuntimeError("restore a compatible backup to downgrade event metadata")
