from datetime import datetime

from sqlalchemy import Executable, Text, column, literal_column, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.admission_wait_v1 import DUE_ROWS, OPEN_ROWS
from kora.db.tables import wait
from kora.db.types import SQLITE, UtcTimestamp


def open_wait_statement(dialect: str, node_id: str) -> Executable:
    identity = column("id", Text())
    return (
        select(identity, column("kind", Text()), column("attempt_id", Text()))
        .select_from(
            text("main.wait INDEXED BY ix_wait_open_owner")
            if dialect == SQLITE
            else text("ONLY wait")
        )
        .where(column("node_instance_id", Text()) == node_id, literal_column(OPEN_ROWS))
        .order_by(identity)
        .limit(2)
    )


async def open_wait_matches(
    connection: AsyncConnection, node_id: str, expected_wait_id: str | None
) -> bool:
    with await connection.execute(
        open_wait_statement(connection.dialect.name, node_id)
    ) as result:
        rows = result.all()
    return rows == (
        [] if expected_wait_id is None else [(expected_wait_id, "admission", None)]
    )


async def due_admission_wait(
    connection: AsyncConnection, node_id: str, now: datetime
) -> str | None:
    with await connection.execute(
        open_wait_statement(connection.dialect.name, node_id)
    ) as result:
        rows = result.all()
    if len(rows) != 1:
        return None
    identity, kind, attempt_id = rows[0]
    if kind != "admission" or attempt_id is not None:
        return None
    with await connection.execute(
        select(wait.c.wake_at).where(wait.c.id == identity)
    ) as result:
        wake = result.scalar_one_or_none()
    return str(identity) if wake is not None and wake <= now else None


def due_admission_statement(
    dialect: str,
    now: datetime,
    after: tuple[datetime, str, str] | None = None,
) -> Executable:
    wake = column("wake_at", UtcTimestamp())
    node = column("node_instance_id", Text())
    identity = column("id", Text())
    statement = (
        select(wake, node, identity)
        .select_from(
            text("main.wait INDEXED BY ix_wait_admission_due")
            if dialect == SQLITE
            else text("ONLY wait")
        )
        .where(literal_column(DUE_ROWS), wake <= now)
        .order_by(wake, node, identity)
        .limit(100)
    )
    return (
        statement
        if after is None
        else statement.where(tuple_(wake, node, identity) > after)
    )
