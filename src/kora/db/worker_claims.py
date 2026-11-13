from datetime import datetime

from sqlalchemy import (
    Boolean,
    Table,
    Text,
    and_,
    case,
    cast,
    func,
    literal,
    literal_column,
    select,
)
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.grant_history import row_fits
from kora.db.identifiers import is_identifier
from kora.db.tables import attempt
from kora.db.transactions import writer_transaction
from kora.db.types import SQLITE
from kora.db.worker_claims_v1 import OPEN_ROWS, UNKNOWN_ROWS


async def claim_row(
    connection: AsyncConnection,
    owner: Table,
    predicate: ColumnElement[bool],
    fields: tuple[str, ...],
    maximum: int,
    *,
    lock: bool = False,
    skip_locked: bool = False,
) -> RowMapping | None:
    if type(maximum) is not int or maximum <= 0:
        raise ClaimSourcesRejected()
    valid = and_(
        *(
            owner.c[name].is_(None)
            | (func.octet_length(cast(owner.c[name], Text())) <= 4096)
            for name in fields
        )
    )
    if connection.dialect.name == SQLITE:
        valid = and_(
            valid,
            *(
                owner.c[name].is_(None)
                | and_(
                    func.typeof(owner.c[name]) == "integer", owner.c[name].in_((0, 1))
                )
                for name in fields
                if isinstance(owner.c[name].type, Boolean)
            ),
        )
        valid = case((row_fits(owner, maximum), valid), else_=False)
    statement = select(
        valid.label("admitted"),
        *(case((valid, owner.c[name])).label(name) for name in fields),
    ).where(predicate)
    if lock:
        writer_transaction(connection).require_active()
        statement = statement.with_for_update(skip_locked=skip_locked)
    row = (await connection.execute(statement)).mappings().one_or_none()
    if row is not None and row["admitted"] is not True:
        raise ClaimSourcesRejected()
    return row


def live_worker_slots(
    worker_id: str, now: datetime, *, unknown: bool
) -> ColumnElement[bool]:
    return and_(
        attempt.c.worker_id == worker_id,
        attempt.c.lease_until > now,
        literal_column(UNKNOWN_ROWS if unknown else OPEN_ROWS),
    )


async def unknown_worker_slot(
    connection: AsyncConnection, worker_id: str, now: datetime
) -> str | None:
    value = await connection.scalar(
        select(case((func.octet_length(attempt.c.id) <= 104, attempt.c.id), else_=""))
        .where(live_worker_slots(worker_id, now, unknown=True))
        .order_by(attempt.c.lease_until, attempt.c.id)
        .limit(1)
    )
    if value is not None and not is_identifier(value):
        raise ClaimSourcesRejected()
    return value


async def worker_slot_available(
    connection: AsyncConnection, worker_id: str, capacity: int, now: datetime
) -> bool:
    writer_transaction(connection).require_active()
    if type(capacity) is not int or not 0 < capacity < 2**31:
        raise ClaimSourcesRejected()
    if await unknown_worker_slot(connection, worker_id, now) is not None:
        return False
    occupied = (
        select(literal(1))
        .where(live_worker_slots(worker_id, now, unknown=False))
        .limit(capacity)
        .subquery()
    )
    count = await connection.scalar(select(func.count()).select_from(occupied))
    return count is not None and count < capacity
