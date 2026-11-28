from sqlalchemy import Insert, exists, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.first_claim_basis import read_position_revision
from kora.db.run_event_position_reads import (
    LEGACY_REVISIONS,
    position_identifier,
    read_position_transition,
)
from kora.db.run_event_position_v1 import (
    MAX_POSITION,
    POSITION_REVISION,
    RunEventPositionRejected,
)
from kora.db.tables import event, run, run_event_position, run_event_position_state
from kora.engine.locks import TransitionLocks


async def append_run_event(
    connection: AsyncConnection,
    locks: TransitionLocks,
    run_id: str,
    statement: Insert,
    *,
    new_run: bool = False,
) -> None:
    locks.require_connection(connection)
    locks.require(run_id)
    if statement.table is not event:
        raise RunEventPositionRejected()
    revision = await read_position_revision(connection)
    if revision in LEGACY_REVISIONS:
        (await connection.execute(statement)).close()
        return
    if (
        revision != POSITION_REVISION
        or not (await read_position_transition(connection)).ready
    ):
        raise RunEventPositionRejected()
    with await connection.execute(
        statement.returning(event.c.id, event.c.run_id, event.c.namespace_id)
    ) as result:
        row = result.one()
        event_id = position_identifier(row[0])
        namespace_id = position_identifier(row[2])
        if position_identifier(row[1]) != run_id:
            raise RunEventPositionRejected()
    state = run_event_position_state
    if new_run:
        (
            await connection.execute(
                insert(state).from_select(
                    ("run_id", "namespace_id", "high_water", "ready"),
                    select(
                        run.c.id, run.c.namespace_id, literal(0), literal(True)
                    ).where(run.c.id == run_id, run.c.namespace_id == namespace_id),
                )
            )
        ).close()
    positions = run_event_position.c
    tail = (
        select(positions.position)
        .where(positions.run_id == run_id)
        .order_by(positions.position.desc())
        .limit(1)
        .scalar_subquery()
    )
    position: object = await connection.scalar(
        update(state)
        .where(
            state.c.run_id == run_id,
            state.c.namespace_id == namespace_id,
            state.c.ready.is_(True),
            state.c.high_water < MAX_POSITION,
            state.c.high_water == func.coalesce(tail, 0),
            exists().where(run.c.id == run_id, run.c.namespace_id == namespace_id),
        )
        .values(high_water=state.c.high_water + 1)
        .returning(state.c.high_water)
    )
    if type(position) is not int or not 0 < position <= MAX_POSITION:
        raise RunEventPositionRejected()
    (
        await connection.execute(
            insert(run_event_position).values(
                run_id=run_id, position=position, event_id=event_id
            )
        )
    ).close()
