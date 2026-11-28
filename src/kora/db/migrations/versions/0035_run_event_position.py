from collections.abc import Sequence

from alembic import op
from sqlalchemy import insert

from kora.db.run_event_maintenance_admission import position_migration_authority
from kora.db.run_event_page_v1 import RUN_EVENT_REVISION
from kora.db.run_event_position_v1 import (
    POSITION_REVISION,
    POSITION_TABLE,
    POSITION_TRANSITION,
    RUN_POSITION_STATE,
    position_elements,
    position_state_elements,
    position_transition_elements,
)
from kora.db.types import POSTGRESQL, SQLITE

revision: str = POSITION_REVISION
down_revision: str | None = RUN_EVENT_REVISION
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    authority = position_migration_authority(connection)
    dialect = connection.dialect.name
    if dialect not in (POSTGRESQL, SQLITE):
        raise RuntimeError("unsupported run-event position migration backend")
    op.create_table(POSITION_TABLE, *position_elements(dialect))
    op.create_table(RUN_POSITION_STATE, *position_state_elements(dialect))
    transition = op.create_table(POSITION_TRANSITION, *position_transition_elements())
    operation, proof = (None, None) if authority is None else authority
    connection.execute(
        insert(transition).values(
            id=1, operation=operation, proof=proof, ready=authority is None
        )
    ).close()


def downgrade() -> None:
    raise RuntimeError("restore the matching backup to downgrade event positions")
