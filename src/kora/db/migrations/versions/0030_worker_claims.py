from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from kora.db.types import POSTGRESQL
from kora.db.worker_claims_v1 import CLAIM_INDEXES, claim_columns

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == POSTGRESQL
    for owner, field in claim_columns():
        op.add_column(owner, field, if_not_exists=True if postgres else None)
    for name, owner, fields, predicate, unique in CLAIM_INDEXES:
        condition = None if predicate is None else text(predicate)
        if not postgres:
            op.create_index(
                name, owner, list(fields), unique=unique, sqlite_where=condition
            )
            continue
        state = (
            op.get_bind()
            .execute(
                text(
                    "SELECT indisvalid, indrelid = to_regclass(:owner), "
                    "indnatts, indisunique, "
                    "ARRAY(SELECT pg_get_indexdef(indexrelid, n, true) "
                    "FROM generate_series(1, indnkeyatts) AS n), "
                    "pg_get_expr(indpred, indrelid) "
                    "FROM pg_index WHERE indexrelid = to_regclass(:name)"
                ),
                {"name": name, "owner": owner},
            )
            .one_or_none()
        )
        if state is not None and tuple(state[1:]) != (
            True,
            len(fields),
            unique,
            list(fields),
            None if predicate is None else f"({predicate})",
        ):
            raise RuntimeError("worker Claim index conflicts with its migration")
        if state is not None and state[0] is True:
            continue
        with op.get_context().autocommit_block():
            if state is not None:
                op.drop_index(name, table_name=owner, postgresql_concurrently=True)
            op.create_index(
                name,
                owner,
                list(fields),
                unique=unique,
                postgresql_where=condition,
                postgresql_concurrently=True,
            )


def downgrade() -> None:
    raise RuntimeError(
        "worker Claims cannot be downgraded; restore a compatible backup"
    )
