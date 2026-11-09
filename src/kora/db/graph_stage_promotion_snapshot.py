import asyncio
import math
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import (
    Database,
    DatabaseSnapshotFailure,
    DatabaseSnapshotRejected,
)
from kora.db.graph_deployment_stage_reads import (
    GraphStageInventory,
    GraphStageInventoryReadFailed,
    GraphStageInventoryRejected,
    GraphStageScope,
    read_graph_stage_inventory,
)
from kora.db.graph_deployment_stage_v1 import MAX_STAGE_OCCURRENCES
from kora.db.graph_preparation_reads import (
    GraphPreparationBinding,
    GraphPreparationReadFailed,
    GraphPreparationRejected,
    GraphPreparationSettlement,
    read_graph_preparation_settlement,
    require_graph_preparation_binding,
)
from kora.db.graph_stage_promotion_custody_v1 import MAX_PROMOTION_DOCUMENT_BYTES_V1
from kora.db.graph_stage_promotion_layout import (
    GraphStagePromotionLayout,
    observe_graph_stage_promotion_layout,
)
from kora.db.graph_stage_promotion_projection import (
    MAX_PROMOTION_PAIR_BYTES,
    PromotionDocumentProjectionRejected,
    graph_stage_promotion_document_statement,
)
from kora.db.transactions import raise_private_metadata_error

MAX_PROMOTION_SNAPSHOT_BYTES: int = MAX_STAGE_OCCURRENCES * MAX_PROMOTION_PAIR_BYTES


class GraphStagePromotionSnapshotFailure(StrEnum):
    UNVERIFIED = "graph stage promotion snapshot unverified"
    CAPACITY = "graph stage promotion snapshot allowance exhausted"
    DEADLINE = "graph stage promotion snapshot deadline expired"


class GraphStagePromotionSnapshotRejected(RuntimeError):
    def __init__(self, reason: GraphStagePromotionSnapshotFailure) -> None:
        self.reason: GraphStagePromotionSnapshotFailure = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True, repr=False)
class GraphStagePromotionDocuments:
    stage_id: str
    request_bytes: bytes | None
    observation_bytes: bytes | None


@dataclass(frozen=True, slots=True, repr=False)
class GraphStagePromotionSnapshot:
    preparation: GraphPreparationSettlement
    inventory: GraphStageInventory
    documents: tuple[GraphStagePromotionDocuments, ...]


def _unverified() -> GraphStagePromotionSnapshotRejected:
    return GraphStagePromotionSnapshotRejected(
        GraphStagePromotionSnapshotFailure.UNVERIFIED
    )


def _require_time(deadline: float) -> None:
    if asyncio.get_running_loop().time() >= deadline:
        raise GraphStagePromotionSnapshotRejected(
            GraphStagePromotionSnapshotFailure.DEADLINE
        )


def _clear_failure(error: BaseException) -> None:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None


def _document_width(width: object) -> int:
    if type(width) is not int or not 0 < width <= MAX_PROMOTION_DOCUMENT_BYTES_V1:
        raise _unverified()
    return width


async def _project_stage(
    connection: AsyncConnection, stage_id: str, allowance: int
) -> tuple[GraphStagePromotionDocuments, int]:
    statement = graph_stage_promotion_document_statement(
        connection.dialect.name, stage_id, max_document_bytes=allowance
    )
    result = await connection.execute(statement)
    try:
        row: tuple[object, ...] = tuple(result.one())
    finally:
        result.close()
    intent, settlement, intent_width, settlement_width, request, observation = row
    if type(intent) is not bool or type(settlement) is not bool:
        raise _unverified()
    if not intent:
        if settlement or any(value is not None for value in row[2:]):
            raise _unverified()
        return GraphStagePromotionDocuments(stage_id, None, None), 0
    request_size = _document_width(intent_width)
    if settlement:
        observation_size = _document_width(settlement_width)
    else:
        if settlement_width is not None or observation is not None:
            raise _unverified()
        observation_size = 0
    transferred = request_size + observation_size
    if transferred > allowance:
        if request is not None or observation is not None:
            raise _unverified()
        raise GraphStagePromotionSnapshotRejected(
            GraphStagePromotionSnapshotFailure.CAPACITY
        )
    if type(request) is not bytes or len(request) != request_size:
        raise _unverified()
    if settlement and (
        type(observation) is not bytes or len(observation) != observation_size
    ):
        raise _unverified()
    if observation is not None and type(observation) is not bytes:
        raise _unverified()
    return GraphStagePromotionDocuments(stage_id, request, observation), transferred


async def _collect(
    connection: AsyncConnection,
    binding: GraphPreparationBinding,
    max_stages: int,
    max_document_bytes: int,
    deadline: float,
) -> GraphStagePromotionSnapshot:
    _require_time(deadline)
    preparation = await read_graph_preparation_settlement(connection, binding)
    if preparation is None:
        raise _unverified()
    _require_time(deadline)
    if (
        await observe_graph_stage_promotion_layout(connection)
        is not GraphStagePromotionLayout.VERIFIED
    ):
        raise _unverified()
    _require_time(deadline)
    inventory = await read_graph_stage_inventory(
        connection, GraphStageScope(binding.preparation_id, binding.namespace_id)
    )
    if len(inventory.stages) > max_stages:
        raise GraphStagePromotionSnapshotRejected(
            GraphStagePromotionSnapshotFailure.CAPACITY
        )
    documents: list[GraphStagePromotionDocuments] = []
    remaining = max_document_bytes
    for stage in inventory.stages:
        _require_time(deadline)
        document, transferred = await _project_stage(
            connection, stage.stage_id, min(remaining, MAX_PROMOTION_PAIR_BYTES)
        )
        documents.append(document)
        remaining -= transferred
    return GraphStagePromotionSnapshot(preparation, inventory, tuple(documents))


async def _read_current(
    connection: AsyncConnection,
    binding: GraphPreparationBinding,
    max_stages: int,
    max_document_bytes: int,
    deadline: float,
) -> GraphStagePromotionSnapshot:
    try:
        return await _collect(
            connection, binding, max_stages, max_document_bytes, deadline
        )
    except GraphStagePromotionSnapshotRejected as error:
        reason = error.reason
        _clear_failure(error)
    except (
        GraphPreparationReadFailed,
        GraphPreparationRejected,
        GraphStageInventoryReadFailed,
        GraphStageInventoryRejected,
        PromotionDocumentProjectionRejected,
        SQLAlchemyError,
        OSError,
    ) as error:
        reason = GraphStagePromotionSnapshotFailure.UNVERIFIED
        _clear_failure(error)
    del connection, binding, max_stages, max_document_bytes, deadline
    raise_private_metadata_error(GraphStagePromotionSnapshotRejected(reason))


def _require_binding(binding: GraphPreparationBinding) -> None:
    try:
        require_graph_preparation_binding(binding)
    except (GraphPreparationRejected, AttributeError) as error:
        _clear_failure(error)
    else:
        return
    raise _unverified()


async def _read_snapshot(
    database: Database,
    binding: GraphPreparationBinding,
    max_stages: int,
    max_document_bytes: int,
    deadline: float,
) -> GraphStagePromotionSnapshot:
    if (
        type(max_stages) is not int
        or not 0 <= max_stages <= MAX_STAGE_OCCURRENCES
        or type(max_document_bytes) is not int
        or not 0 <= max_document_bytes <= MAX_PROMOTION_SNAPSHOT_BYTES
        or type(deadline) is not float
        or not math.isfinite(deadline)
    ):
        raise _unverified()
    _require_binding(binding)
    _require_time(deadline)
    async with database.read_snapshot(deadline=deadline) as connection:
        result = await _read_current(
            connection, binding, max_stages, max_document_bytes, deadline
        )
    _require_time(deadline)
    return result


async def read_graph_stage_promotion_snapshot(
    database: Database,
    binding: GraphPreparationBinding,
    *,
    max_stages: int,
    max_document_bytes: int,
    deadline: float,
) -> GraphStagePromotionSnapshot:
    try:
        return await _read_snapshot(
            database, binding, max_stages, max_document_bytes, deadline
        )
    except asyncio.CancelledError as error:
        _clear_failure(error)
        del database, binding, max_stages, max_document_bytes, deadline
        raise
    except GraphStagePromotionSnapshotRejected as error:
        reason = error.reason
        _clear_failure(error)
    except DatabaseSnapshotRejected as error:
        reason = (
            GraphStagePromotionSnapshotFailure.DEADLINE
            if error.reason is DatabaseSnapshotFailure.DEADLINE
            else GraphStagePromotionSnapshotFailure.UNVERIFIED
        )
        _clear_failure(error)
    del database, binding, max_stages, max_document_bytes, deadline
    raise_private_metadata_error(GraphStagePromotionSnapshotRejected(reason))
