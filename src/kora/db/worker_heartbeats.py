from sqlalchemy import (
    BigInteger,
    Table,
    Text,
    and_,
    case,
    cast,
    column,
    func,
    select,
    true,
    values,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.grant_history import GrantHistoryRejected, HistoryReadLimits, row_fits
from kora.db.tables import attempt, attempt_dispatcher, node_instance, run
from kora.db.types import POSTGRESQL, SQLITE
from kora.db.worker_claims import claim_row

HEARTBEAT_VALUE_BYTES: int = 8192


async def heartbeat_attempt(
    connection: AsyncConnection,
    attempt_id: str,
    namespaces: tuple[str, ...],
    fields: tuple[str, ...],
    limits: HistoryReadLimits,
) -> dict[str, object] | None:
    scoped = (
        select(node_instance.c.id)
        .join(run)
        .where(
            node_instance.c.id == attempt.c.node_instance_id,
            run.c.namespace_id.in_(namespaces),
        )
        .exists()
    )
    row = await claim_row(
        connection,
        attempt,
        (attempt.c.id == attempt_id) & scoped,
        fields,
        limits.attempt_row,
        field_bytes=2 * HEARTBEAT_VALUE_BYTES,
    )
    return None if row is None else {name: row[name] for name in fields}


async def heartbeat_dispatchers(
    connection: AsyncConnection,
    attempt_id: str,
    keys: tuple[tuple[str, int], ...],
    fields: tuple[str, ...],
    limits: HistoryReadLimits,
) -> tuple[dict[str, object], ...]:
    if not keys:
        return ()
    if len(keys) > 128 or len(set(keys)) != len(keys):
        raise GrantHistoryRejected()
    owner = attempt_dispatcher
    valid = and_(
        *(
            owner.c[name].is_(None)
            | (
                func.octet_length(cast(owner.c[name], Text()))
                <= 2 * HEARTBEAT_VALUE_BYTES
            )
            for name in fields
        )
    )
    if connection.dialect.name == SQLITE:
        valid = case((row_fits(owner, limits.dispatcher_row), valid), else_=False)
    submitted = (
        values(column("dispatcher_id", Text), column("epoch", BigInteger))
        .data(list(keys))
        .cte("submitted")
    )
    statement = select(
        valid.label("admitted"),
        *(case((valid, owner.c[name])).label(name) for name in fields),
    ).where(
        owner.c.attempt_id == attempt_id,
        owner.c.id == submitted.c.dispatcher_id,
        owner.c.lease_epoch == submitted.c.epoch,
    )
    if connection.dialect.name == POSTGRESQL:
        matched = statement.correlate(submitted).limit(1).lateral()
        statement = select(*matched.c).select_from(submitted.join(matched, true()))
    else:
        statement = statement.select_from(submitted.join(owner, true())).limit(
            len(keys)
        )
    with await connection.execute(statement) as result:
        rows = tuple(result.mappings())
    if any(row["admitted"] is not True for row in rows):
        raise GrantHistoryRejected()
    return tuple({name: row[name] for name in fields} for row in rows)


async def reserve_heartbeat_row(
    connection: AsyncConnection,
    owner: Table,
    identity: str,
    maximum: int,
    replaced: tuple[str, ...],
    added: int,
) -> None:
    if (
        connection.dialect.name == SQLITE
        and await connection.scalar(
            select(row_fits(owner, maximum, replaced, added)).where(
                owner.c.id == identity
            )
        )
        is not True
    ):
        raise GrantHistoryRejected()
