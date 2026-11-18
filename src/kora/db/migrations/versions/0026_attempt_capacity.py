from collections.abc import Sequence

from alembic import op
from sqlalchemy import Boolean, literal_column, text

from kora.db.attempt_capacity_v1 import INDEXES, capacity_index_dialect
from kora.db.types import POSTGRESQL

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _create_index(
    name: str,
    table: str,
    columns: tuple[str, ...],
    sqlite_predicate: str | None,
    postgres_predicate: str | None,
    expected_predicate: str | None,
) -> None:
    connection = op.get_bind()
    if capacity_index_dialect(name) not in (None, connection.dialect.name):
        return
    predicate = (
        postgres_predicate
        if connection.dialect.name == POSTGRESQL
        else sqlite_predicate
    )
    condition = None if predicate is None else literal_column(predicate, Boolean())
    if connection.dialect.name != POSTGRESQL:
        op.create_index(name, table, columns, sqlite_where=condition)
        return
    with connection.execute(
        text(
            "SELECT indisvalid, indrelid = to_regclass(:table), "
            "indnatts, indisunique, "
            "ARRAY(SELECT pg_get_indexdef(indexrelid, n, true) "
            "FROM generate_series(1, indnkeyatts) AS n), "
            "pg_get_expr(indpred, indrelid) FROM pg_index "
            "WHERE indexrelid = to_regclass(:name)"
        ),
        {"name": name, "table": table},
    ) as result:
        state = result.one_or_none()
    expected = (True, len(columns), False, list(columns), expected_predicate)
    if state is not None and tuple(state[1:]) != expected:
        raise RuntimeError(
            "physical capacity indexes index conflicts with its migration"
        )
    valid = None if state is None else state[0]
    if valid is True:
        return
    with op.get_context().autocommit_block():
        if valid is False:
            op.drop_index(name, table_name=table, postgresql_concurrently=True)
        op.create_index(
            name,
            table,
            columns,
            postgresql_where=condition,
            postgresql_concurrently=True,
        )


def upgrade() -> None:
    for definition in INDEXES:
        _create_index(*definition)


def downgrade() -> None:
    raise RuntimeError(
        "physical capacity indexes cannot be downgraded; restore a compatible backup"
    )
