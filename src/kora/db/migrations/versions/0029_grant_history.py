from collections.abc import Sequence

from alembic import op
from sqlalchemy import BigInteger, text
from sqlalchemy.sql.elements import TextClause

from kora.db.grant_history_v1 import (
    OPEN_GRANT,
    OPEN_INDEX,
    PREFIX_INDEX,
    VERIFIED_PREFIX,
    attempt_history_columns,
    dispatcher_history_columns,
)
from kora.db.types import POSTGRESQL, collated_text

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == POSTGRESQL
    for field in attempt_history_columns():
        op.add_column("attempt", field, if_not_exists=True if postgres else None)
    for field in dispatcher_history_columns():
        op.add_column(
            "attempt_dispatcher", field, if_not_exists=True if postgres else None
        )
    if postgres:
        for owner in ("attempt", "attempt_dispatcher"):
            op.alter_column(owner, "execution_domain_id", type_=collated_text(256))
            op.alter_column(owner, "lease_epoch", type_=BigInteger())
        op.alter_column(
            "attempt_dispatcher", "incarnation_id", type_=collated_text(256)
        )
    for name, predicate in (
        (PREFIX_INDEX, VERIFIED_PREFIX),
        (OPEN_INDEX, OPEN_GRANT),
    ):
        if postgres:
            _postgres_index(name, predicate)
        else:
            op.create_index(
                name,
                "attempt_dispatcher",
                ["attempt_id", "lease_epoch"],
                sqlite_where=predicate,
            )


def _postgres_index(name: str, predicate: TextClause) -> None:
    state = (
        op.get_bind()
        .execute(
            text(
                "SELECT indisvalid, indrelid = 'attempt_dispatcher'::regclass "
                "FROM pg_index WHERE indexrelid = to_regclass(:name)"
            ),
            {"name": name},
        )
        .one_or_none()
    )
    if state is not None and state[1] is not True:
        raise RuntimeError("grant history index belongs to another relation")
    valid = None if state is None else state[0]
    if valid is True:
        return
    with op.get_context().autocommit_block():
        if valid is False:
            op.drop_index(
                name, table_name="attempt_dispatcher", postgresql_concurrently=True
            )
        op.create_index(
            name,
            "attempt_dispatcher",
            ["attempt_id", "lease_epoch"],
            postgresql_where=predicate,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    raise RuntimeError(
        "grant history cannot be downgraded; restore a compatible backup"
    )
