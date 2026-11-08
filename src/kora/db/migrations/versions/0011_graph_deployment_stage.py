from collections.abc import Sequence

from alembic import op

from kora.db.graph_deployment_stage_v1 import (
    OCCURRENCE_TABLE,
    STAGE_TABLE,
    graph_deployment_stage_elements_v1,
    graph_deployment_stage_occurrence_elements_v1,
)

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(STAGE_TABLE, *graph_deployment_stage_elements_v1(dialect))
    op.create_table(
        OCCURRENCE_TABLE, *graph_deployment_stage_occurrence_elements_v1(dialect)
    )


def downgrade() -> None:
    raise RuntimeError(
        "graph deployment stage history cannot be downgraded; "
        "restore a compatible backup"
    )
