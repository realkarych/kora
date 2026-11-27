from alembic import op

from kora.db.custody_frontier_v1 import (
    TABLE_NAME,
    Element,
    custody_frontier_elements_v1,
)
from kora.db.custody_schema import ISOLATED_FRONTIER_REVISION, ISOLATED_FRONTIER_VERSION
from kora.db.types import SQLITE

revision: str = ISOLATED_FRONTIER_REVISION
down_revision: str = "run_event_metadata_0034"


def frontier_elements() -> tuple[Element, ...]:
    return custody_frontier_elements_v1(SQLITE, version=ISOLATED_FRONTIER_VERSION)


def upgrade() -> None:
    if op.get_bind().dialect.name != SQLITE:
        raise RuntimeError("The isolated custody frontier requires SQLite.")
    op.create_table(TABLE_NAME, *frontier_elements())


def downgrade() -> None:
    raise RuntimeError("The isolated custody frontier has no downgrade.")
