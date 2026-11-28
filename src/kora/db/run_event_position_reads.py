import re
from dataclasses import dataclass

from sqlalchemy import (
    ColumnElement,
    Connection,
    String,
    and_,
    case,
    func,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.identifiers import is_identifier
from kora.db.run_event_page_v1 import RUN_EVENT_REVISION
from kora.db.run_event_position_v1 import (
    MAX_POSITION,
    RunEventPositionRejected,
)
from kora.db.tables import (
    event,
    run,
    run_event_position_state,
    run_event_position_transition,
)
from kora.db.types import POSTGRESQL, SQLITE

BATCH_EVENTS: int = 256
LEGACY_REVISIONS: frozenset[str] = frozenset(("0032", "0033", RUN_EVENT_REVISION))


@dataclass(frozen=True)
class PositionTransition:
    operation: str | None
    proof: str | None
    after_run_id: str | None
    ready: bool


def position_identifier(value: object) -> str:
    if not isinstance(value, str) or not is_identifier(value):
        raise RunEventPositionRejected()
    return value


async def read_position_transition(connection: AsyncConnection) -> PositionTransition:
    return await connection.run_sync(read_position_transition_sync)


def read_position_transition_sync(connection: Connection) -> PositionTransition:
    table = run_event_position_transition
    with connection.execute(
        select(
            bounded_position_text(connection, table.c.operation, 32),
            bounded_position_text(connection, table.c.proof, 64),
            bounded_position_text(connection, table.c.after_run_id, 26),
            table.c.ready,
        ).where(table.c.id == 1, table.c.ready.in_((False, True)))
    ) as result:
        row = result.one_or_none()
    if row is None:
        raise RunEventPositionRejected()
    observed: tuple[object, ...] = tuple(row)
    operation, proof, after_run_id, ready = observed
    if type(ready) is not bool:
        raise RunEventPositionRejected()
    if after_run_id is not None:
        after_run_id = position_identifier(after_run_id)
    if operation is None and proof is None and ready and after_run_id is None:
        return PositionTransition(None, None, None, True)
    if (
        not isinstance(operation, str)
        or re.fullmatch("[0-9a-f]{32}", operation) is None
        or not isinstance(proof, str)
        or re.fullmatch("[0-9a-f]{64}", proof) is None
    ):
        raise RunEventPositionRejected()
    return PositionTransition(operation, proof, after_run_id, ready)


def bounded_position_text(
    connection: AsyncConnection | Connection, column: ColumnElement[str], width: int
) -> ColumnElement[str]:
    size = func.octet_length(column)
    fits = size <= width
    if connection.dialect.name == SQLITE:
        fits = and_(func.typeof(column) == "text", size <= 2 * width)
    return case((column.is_(None), None), (fits, column), else_="~").cast(String)


def position_integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_POSITION:
        raise RunEventPositionRejected()
    return value


@dataclass(frozen=True)
class FrozenRun:
    id: str
    namespace_id: str


@dataclass(frozen=True)
class RunPositionCheckpoint:
    high_water: int
    legacy_event_id: str | None
    ready: bool


async def next_frozen_run(
    connection: AsyncConnection, after: str | None
) -> FrozenRun | None:
    statement = (
        select(
            bounded_position_text(connection, run.c.id, 26),
            bounded_position_text(connection, run.c.namespace_id, 26),
        )
        .order_by(run.c.id)
        .limit(1)
    )
    if after is not None:
        statement = statement.where(run.c.id > after)
    with await connection.execute(statement) as result:
        row = result.one_or_none()
    if row is None:
        return None
    return FrozenRun(position_identifier(row[0]), position_identifier(row[1]))


async def read_run_position_checkpoint(
    connection: AsyncConnection, frozen: FrozenRun
) -> RunPositionCheckpoint | None:
    cols = run_event_position_state.c
    with await connection.execute(
        select(
            bounded_position_text(connection, cols.namespace_id, 26),
            cols.high_water,
            bounded_position_text(connection, cols.legacy_event_id, 26),
            cols.ready,
        ).where(cols.run_id == frozen.id, cols.ready.in_((False, True)))
    ) as rows:
        row = rows.one_or_none()
    if row is None:
        return None
    namespace_id, high_water, legacy_event_id, ready = tuple(row)
    high = position_integer(high_water)
    if (
        position_identifier(namespace_id) != frozen.namespace_id
        or (legacy_event_id is None) != (high == 0)
        or type(ready) is not bool
    ):
        raise RunEventPositionRejected()
    key = None if legacy_event_id is None else position_identifier(legacy_event_id)
    return RunPositionCheckpoint(high, key, ready)


async def next_frozen_events(
    connection: AsyncConnection, frozen: FrozenRun, after: str | None
) -> list[str]:
    statement = select(
        bounded_position_text(connection, event.c.id, 26),
        bounded_position_text(connection, event.c.namespace_id, 26),
    ).limit(BATCH_EVENTS)
    if connection.dialect.name == POSTGRESQL:
        key = tuple_(event.c.run_id, event.c.id)
        ordered = statement.where(
            event.c.run_id.between(frozen.id, frozen.id),
        ).order_by(event.c.run_id, event.c.id)
        statement = (
            ordered if after is None else ordered.where(key > (frozen.id, after))
        )
    else:
        statement = statement.where(event.c.run_id == frozen.id).order_by(event.c.id)
        if after is not None:
            statement = statement.where(event.c.id > after)
    with await connection.execute(statement) as rows:
        values: list[str] = []
        for identity, namespace in rows:
            if position_identifier(namespace) != frozen.namespace_id:
                raise RunEventPositionRejected()
            values.append(position_identifier(identity))
        return values
