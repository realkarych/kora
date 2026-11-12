from collections.abc import Sequence

from alembic import op

from kora.db.types import POSTGRESQL, collated_text
from kora.db.worker_registration_v1 import (
    DOMAIN_TABLE,
    EVENT_NAMESPACE_SCOPE,
    INCARNATION_TABLE,
    domain_elements,
    incarnation_elements,
)

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == POSTGRESQL:
        for column in ("incarnation_id", "execution_domain_id"):
            op.alter_column("worker", column, type_=collated_text(256))
    op.create_table(DOMAIN_TABLE, *domain_elements())
    op.create_table(INCARNATION_TABLE, *incarnation_elements())
    with op.batch_alter_table("event", reflect_kwargs={"resolve_fks": False}) as batch:
        batch.alter_column(
            "namespace_id", existing_type=collated_text(26), nullable=True
        )
        batch.create_check_constraint(
            op.f("ck_event_namespace_scope"), EVENT_NAMESPACE_SCOPE
        )


def downgrade() -> None:
    raise RuntimeError(
        "worker registration cannot be downgraded; restore a compatible backup"
    )
