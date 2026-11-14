from datetime import datetime

from sqlalchemy import Text, case, column, func, select, text, tuple_, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement, TextClause

from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.identifiers import is_identifier
from kora.db.tables import attempt, worker
from kora.db.transactions import writer_transaction
from kora.db.types import SQLITE
from kora.db.worker_claim_poll_v1 import CANDIDATE_PREDICATE

MAX_CANDIDATES: int = 32


def candidate_page(
    connection: AsyncConnection, runtime: str, after: str | None
) -> tuple[ColumnElement[bool] | TextClause, ...]:
    runtime_key, identity = column("runtime", Text()), column("id", Text())
    if connection.dialect.name == SQLITE:
        conditions = (runtime_key == runtime, CANDIDATE_PREDICATE)
        return conditions if after is None else (*conditions, identity > after)
    conditions = (runtime_key >= runtime, runtime_key <= runtime, CANDIDATE_PREDICATE)
    return (
        conditions
        if after is None
        else (*conditions, tuple_(runtime_key, identity) > (runtime, after))
    )


async def claim_candidates(
    connection: AsyncConnection, runtime: str, after: str | None
) -> tuple[str, ...]:
    identity = column("id", Text())
    source = (
        text("main.attempt INDEXED BY ix_attempt_claim_candidate")
        if connection.dialect.name == SQLITE
        else attempt
    )
    rows = await connection.scalars(
        select(case((func.octet_length(identity) <= 104, identity)))
        .select_from(source)
        .where(*candidate_page(connection, runtime, after))
        .order_by(column("runtime", Text()), identity)
        .limit(MAX_CANDIDATES)
    )
    result = tuple(rows)
    if any(type(value) is not str or not is_identifier(value) for value in result):
        raise ClaimSourcesRejected()
    return tuple(str(value) for value in result)


async def advance_claim_cursor(
    connection: AsyncConnection,
    worker_id: str,
    previous: str | None,
    generation: int,
    position: str | None,
    now: datetime,
) -> bool:
    writer_transaction(connection).require_active()
    if type(generation) is not int or not 0 <= generation < 2**63 - 1:
        raise ClaimSourcesRejected()
    result = await connection.execute(
        update(worker)
        .where(
            worker.c.id == worker_id,
            worker.c.claim_cursor.is_(None)
            if previous is None
            else worker.c.claim_cursor == previous,
            worker.c.claim_cursor_generation == generation,
        )
        .values(
            claim_cursor=position,
            claim_cursor_generation=generation + 1,
            updated_at=now,
        )
    )
    return result.rowcount == 1
