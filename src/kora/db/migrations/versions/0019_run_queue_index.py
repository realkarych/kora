from collections.abc import Sequence

from alembic import op
from sqlalchemy import Connection, literal_column

from kora.db.run_queue_index_v1 import (
    ACTIVE_QUEUE_ROWS,
    POSTGRES_QUEUE_INDEX,
    POSTGRES_QUEUE_INDEX_STATE,
    POSTGRES_QUEUE_KEY,
    QUEUE_COLUMNS,
    SQLITE_QUEUE_INDEX,
    SQLITE_QUEUE_KEY,
    SQLITE_QUEUE_ROWS,
    postgres_queue_index_definition,
)
from kora.db.types import POSTGRESQL, SQLITE

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _index_state(connection: Connection) -> bool | None:
    with connection.execute(
        POSTGRES_QUEUE_INDEX_STATE, {"name": POSTGRES_QUEUE_INDEX}
    ) as result:
        row = result.one_or_none()
    if row is None:
        return None
    schema, definition, valid, ready, live, same_table = row
    if (
        type(schema) is not str
        or definition != postgres_queue_index_definition(schema)
        or same_table is not True
    ):
        raise RuntimeError("run queue migration found an incompatible schema object")
    return valid is ready is live is True


def _upgrade_postgresql(connection: Connection) -> None:
    state = _index_state(connection)
    if state is not True:
        with op.get_context().autocommit_block():
            if state is False:
                connection.exec_driver_sql(
                    f"DROP INDEX CONCURRENTLY {POSTGRES_QUEUE_INDEX}"
                )
            connection.exec_driver_sql(
                f"CREATE INDEX CONCURRENTLY {POSTGRES_QUEUE_INDEX} ON run "
                f"({', '.join(QUEUE_COLUMNS)}, ({POSTGRES_QUEUE_KEY})) "
                f"WHERE {ACTIVE_QUEUE_ROWS}"
            )
    if _index_state(connection) is not True:
        raise RuntimeError("run queue migration could not verify its index")


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == POSTGRESQL:
        _upgrade_postgresql(connection)
    elif connection.dialect.name == SQLITE:
        op.create_index(
            SQLITE_QUEUE_INDEX,
            "run",
            [*QUEUE_COLUMNS, literal_column(f"({SQLITE_QUEUE_KEY})")],
            sqlite_where=literal_column(SQLITE_QUEUE_ROWS),
        )
    else:
        raise RuntimeError("unsupported run queue migration backend")


def downgrade() -> None:
    raise RuntimeError(
        "run queue migration cannot be downgraded; restore a compatible backup"
    )
