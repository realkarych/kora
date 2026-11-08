from collections.abc import Sequence

from alembic import op

from kora.db.graph_stage_promotion_custody_v1 import (
    INTENT_TABLE,
    SETTLEMENT_TABLE,
    graph_stage_promotion_intent_elements_v1,
    graph_stage_promotion_settlement_elements_v1,
)

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(INTENT_TABLE, *graph_stage_promotion_intent_elements_v1(dialect))
    op.create_table(
        SETTLEMENT_TABLE, *graph_stage_promotion_settlement_elements_v1(dialect)
    )


def downgrade() -> None:
    raise RuntimeError(
        "graph stage promotion custody cannot be downgraded. "
        "Restore a compatible backup."
    )
