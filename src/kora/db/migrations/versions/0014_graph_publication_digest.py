from collections.abc import Sequence

from alembic import op
from sqlalchemy import Connection, literal_column, text

from kora.db.graph_publication_digest_v1 import (
    DIGEST_INDEX,
    INVALID_ID_INDEX,
    LOCATOR_COLUMNS,
    SQLITE_INVALID,
    SQLITE_VALID,
    VALID_COLUMNS,
)
from kora.db.types import POSTGRESQL, SQLITE

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _index_state(connection: Connection) -> bool | None:
    row = connection.execute(
        text(
            "SELECT pg_get_indexdef(c.oid), i.indisvalid, i.indisready, i.indislive "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE n.nspname = current_schema() AND c.relname = :name"
        ),
        {"name": DIGEST_INDEX},
    ).one_or_none()
    if row is None:
        return None
    schema = str(connection.execute(text("SELECT current_schema()")).scalar_one())
    quoted = connection.dialect.identifier_preparer.quote(schema)
    expected = (
        f"CREATE INDEX {DIGEST_INDEX} ON {quoted}.graph_version "
        f"USING btree ({', '.join(VALID_COLUMNS)})"
    )
    if row[0] != expected:
        raise RuntimeError("graph digest migration found an incompatible schema object")
    return all(state is True for state in row[1:])


def _upgrade_postgresql(connection: Connection) -> None:
    state = _index_state(connection)
    if state is not True:
        with op.get_context().autocommit_block():
            if state is False:
                connection.exec_driver_sql(f"DROP INDEX CONCURRENTLY {DIGEST_INDEX}")
            connection.exec_driver_sql(
                f"CREATE INDEX CONCURRENTLY {DIGEST_INDEX} ON graph_version "
                f"({', '.join(VALID_COLUMNS)})"
            )
    if _index_state(connection) is not True:
        raise RuntimeError("graph digest migration could not verify its index")


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == POSTGRESQL:
        _upgrade_postgresql(connection)
    elif connection.dialect.name == SQLITE:
        op.create_index(
            DIGEST_INDEX,
            "graph_version",
            VALID_COLUMNS,
            sqlite_where=literal_column(SQLITE_VALID),
        )
        op.create_index(
            INVALID_ID_INDEX,
            "graph_version",
            LOCATOR_COLUMNS,
            sqlite_where=literal_column(SQLITE_INVALID),
        )
    else:
        raise RuntimeError("unsupported graph digest migration backend")


def downgrade() -> None:
    raise RuntimeError(
        "graph digest migration cannot be downgraded; restore a compatible backup"
    )
