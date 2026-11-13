from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from kora.db.deadline_discovery_v1 import PG_CANCEL
from kora.db.types import POSTGRESQL
from kora.db.worker_claim_poll_v1 import (
    CANDIDATE_INDEX,
    CANDIDATE_ROWS,
    cursor_columns,
)

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == POSTGRESQL
    for field in cursor_columns():
        op.add_column("worker", field, if_not_exists=True if postgres else None)
    op.execute(
        text(
            "UPDATE worker SET claim_cursor_generation = 0 "
            "WHERE claim_cursor_generation IS NULL"
        )
    )
    if not postgres:
        op.create_index(
            CANDIDATE_INDEX,
            "attempt",
            ["runtime", "id"],
            sqlite_where=text(CANDIDATE_ROWS),
        )
        return
    state = (
        op.get_bind()
        .execute(
            text(
                "SELECT indisvalid, indrelid = to_regclass('attempt'), "
                "indnatts, indisunique, "
                "ARRAY(SELECT pg_get_indexdef(indexrelid, n, true) "
                "FROM generate_series(1, indnkeyatts) AS n), "
                "pg_get_expr(indpred, indrelid) "
                "FROM pg_index WHERE indexrelid = to_regclass(:name)"
            ),
            {"name": CANDIDATE_INDEX},
        )
        .one_or_none()
    )
    if state is not None and tuple(state[1:]) != (
        True,
        2,
        False,
        ["runtime", "id"],
        f"(({PG_CANCEL} OR (recovery_required IS TRUE)) IS TRUE)",
    ):
        raise RuntimeError("worker Claim candidate index conflicts with its migration")
    if state is not None and state[0] is True:
        return
    with op.get_context().autocommit_block():
        if state is not None:
            op.drop_index(
                CANDIDATE_INDEX, table_name="attempt", postgresql_concurrently=True
            )
        op.create_index(
            CANDIDATE_INDEX,
            "attempt",
            ["runtime", "id"],
            postgresql_where=text(CANDIDATE_ROWS),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    raise RuntimeError(
        "worker Claim polling cannot be downgraded; restore a compatible backup"
    )
