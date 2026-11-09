from dataclasses import dataclass
from functools import partial

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from kora.db.connection import nothing, require_snapshot_time, start_snapshot_phase
from kora.db.graph_deployment_stage_reads import (
    PAYLOAD_DIGEST_WIDTH,
    STORE_ID_WIDTH,
    GraphStage,
)
from kora.db.identifiers import is_identifier
from kora.db.maintenance import (
    PROMOTION_INTENT_TEXT_WIDTHS,
    PromotionIntentScalarsRejected,
    observe_sqlite_promotion_intent,
    require_promotion_intent_width,
)
from kora.db.types import POSTGRESQL, SQLITE

SQLITE_ENCODING: TextClause = text("PRAGMA main.encoding")
SQLITE_SCALARS: TextClause = text(
    """
SELECT store_id, payload_digest, fence_incarnation,
       operation_id, request_digest, content_generation
  FROM main.graph_deployment_stage_promotion_intent
       INDEXED BY sqlite_autoindex_graph_deployment_stage_promotion_intent_1
 WHERE stage_id COLLATE BINARY = :stage_id
"""
)
POSTGRES_METADATA: TextClause = text(
    """
SELECT pg_catalog.octet_length(store_id), pg_catalog.octet_length(payload_digest),
       pg_catalog.octet_length(operation_id), pg_catalog.octet_length(request_digest)
  FROM graph_deployment_stage_promotion_intent
 WHERE stage_id OPERATOR(pg_catalog.=) CAST(:stage_id AS pg_catalog.text)
"""
)
POSTGRES_SCALARS: TextClause = text(
    """
SELECT store_id, payload_digest, fence_incarnation,
       operation_id, request_digest, content_generation
  FROM graph_deployment_stage_promotion_intent
 WHERE stage_id OPERATOR(pg_catalog.=) CAST(:stage_id AS pg_catalog.text)
"""
)


@dataclass(frozen=True, slots=True, repr=False)
class GraphStagePromotionIntentScalars:
    store_id: str
    payload_digest: str
    fence_incarnation: int
    operation_id: str
    request_digest: str
    content_generation: int


async def read_intent_text_width_factor(connection: AsyncConnection) -> int:
    if connection.dialect.name == POSTGRESQL:
        return 1
    if connection.dialect.name != SQLITE:
        raise PromotionIntentScalarsRejected()
    encoding: object = await connection.scalar(SQLITE_ENCODING)
    if type(encoding) is not str:
        raise PromotionIntentScalarsRejected()
    if encoding == "UTF-8":
        return 1
    if encoding in ("UTF-16le", "UTF-16be"):
        return 2
    raise PromotionIntentScalarsRejected()


async def _sqlite_intent_present(
    connection: AsyncConnection, stage_id: str, width_factor: int, deadline: float
) -> bool:
    async def observe() -> bool:
        return await observe_sqlite_promotion_intent(
            connection, stage_id, width_factor, partial(require_snapshot_time, deadline)
        )

    return await start_snapshot_phase(observe, nothing)


async def _selected_row(
    connection: AsyncConnection, statement: TextClause, stage_id: str
) -> tuple[object, ...] | None:
    result = await connection.execute(statement, {"stage_id": stage_id})
    try:
        row = result.one_or_none()
        return None if row is None else tuple(row)
    finally:
        result.close()


async def _postgres_intent_present(connection: AsyncConnection, stage_id: str) -> bool:
    row = await _selected_row(connection, POSTGRES_METADATA, stage_id)
    if row is None:
        return False
    if len(row) != len(PROMOTION_INTENT_TEXT_WIDTHS):
        raise PromotionIntentScalarsRejected()
    for width, (_, lower, upper) in zip(row, PROMOTION_INTENT_TEXT_WIDTHS, strict=True):
        require_promotion_intent_width(width, lower, upper, 1)
    return True


def _printable(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= STORE_ID_WIDTH
        and all("!" <= character <= "~" for character in value)
    )


def _digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == PAYLOAD_DIGEST_WIDTH
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _checked_scalars(
    row: tuple[object, ...], stage: GraphStage
) -> GraphStagePromotionIntentScalars:
    if len(row) != 6:
        raise PromotionIntentScalarsRejected()
    store, payload, fence, operation, request, generation = row
    if (
        type(store) is not str
        or not _printable(store)
        or store != stage.store_id
        or type(payload) is not str
        or not _digest(payload)
        or payload != stage.payload_digest
        or type(fence) is not int
        or not 0 < fence < 2**63
        or fence != stage.fence_incarnation
        or type(operation) is not str
        or not _printable(operation)
        or type(request) is not str
        or not _digest(request)
        or type(generation) is not int
        or not 0 <= generation < 2**63
    ):
        raise PromotionIntentScalarsRejected()
    return GraphStagePromotionIntentScalars(
        store, payload, fence, operation, request, generation
    )


async def read_graph_stage_promotion_intent_scalars(
    connection: AsyncConnection, stage: GraphStage, width_factor: int, deadline: float
) -> GraphStagePromotionIntentScalars | None:
    require_snapshot_time(deadline)
    transaction = connection.get_transaction()
    if (
        not is_identifier(stage.stage_id)
        or connection.closed
        or connection.invalidated
        or transaction is None
        or not transaction.is_active
        or connection.in_nested_transaction()
    ):
        raise PromotionIntentScalarsRejected()
    if connection.dialect.name == SQLITE:
        present = await _sqlite_intent_present(
            connection, stage.stage_id, width_factor, deadline
        )
        statement = SQLITE_SCALARS
    elif connection.dialect.name == POSTGRESQL:
        present = await _postgres_intent_present(connection, stage.stage_id)
        statement = POSTGRES_SCALARS
    else:
        raise PromotionIntentScalarsRejected()
    require_snapshot_time(deadline)
    if (
        connection.invalidated
        or not transaction.is_active
        or connection.get_transaction() is not transaction
    ):
        raise PromotionIntentScalarsRejected()
    if not present:
        return None
    row = await _selected_row(connection, statement, stage.stage_id)
    if row is None:
        raise PromotionIntentScalarsRejected()
    return _checked_scalars(row, stage)
