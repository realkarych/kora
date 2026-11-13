from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.tables import worker, worker_incarnation
from kora.db.transactions import writer_transaction
from kora.db.types import POSTGRESQL, SQLITE


async def insert_registration_row(
    connection: AsyncConnection, table: Table, values: dict[str, object]
) -> bool:
    writer_transaction(connection).require_active()
    dialect = connection.dialect.name
    if dialect == POSTGRESQL:
        statement = postgres_insert(table)
    elif dialect == SQLITE:
        statement = sqlite_insert(table)
    else:
        raise RuntimeError("unsupported worker registration backend")
    result = await connection.execute(
        statement.values(**values)
        .on_conflict_do_nothing()
        .returning(*table.primary_key)
    )
    return result.first() is not None


async def locked_worker(connection: AsyncConnection, name: str) -> RowMapping:
    writer_transaction(connection).require_active()
    result = await connection.execute(
        select(worker).where(worker.c.id == name).with_for_update()
    )
    return result.mappings().one()


async def registered_incarnation(
    connection: AsyncConnection, name: str, incarnation: str
) -> RowMapping | None:
    result = await connection.execute(
        select(
            worker_incarnation.c.request_digest, worker_incarnation.c.registered_at
        ).where(
            worker_incarnation.c.worker_id == name,
            worker_incarnation.c.incarnation_id == incarnation,
        )
    )
    return result.mappings().one_or_none()
