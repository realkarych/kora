from collections.abc import Sequence

from alembic import op
from sqlalchemy import literal_column

from kora.db.run_input_locator_v1 import (
    SQLITE_RUN_INPUT_KEY,
    SQLITE_RUN_INPUT_LOCATOR_INDEX,
    SQLITE_RUN_KEY,
    SQLITE_RUN_LOCATOR_INDEX,
)
from kora.db.run_inputs_v1 import TABLE_NAME
from kora.db.types import POSTGRESQL, SQLITE

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == POSTGRESQL:
        return
    if dialect != SQLITE:
        raise RuntimeError("unsupported run input locator migration backend")
    op.create_index(
        SQLITE_RUN_LOCATOR_INDEX,
        "run",
        [literal_column("id COLLATE BINARY")],
        sqlite_where=literal_column(SQLITE_RUN_KEY),
    )
    op.create_index(
        SQLITE_RUN_INPUT_LOCATOR_INDEX,
        TABLE_NAME,
        [literal_column("run_id COLLATE BINARY")],
        sqlite_where=literal_column(SQLITE_RUN_INPUT_KEY),
    )


def downgrade() -> None:
    raise RuntimeError(
        "run input locators cannot be downgraded; restore a compatible backup"
    )
