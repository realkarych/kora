from enum import StrEnum

from sqlalchemy import Integer, insert, literal_column, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.graph_preparation_reads import GraphPreparationBinding
from kora.db.tables import (
    graph_deployment_preparation,
    graph_deployment_preparation_capacity,
)
from kora.db.transactions import raise_private_metadata_error
from kora.engine.locks import TransitionLocks
from kora.protocols.identity_records import MAX_COUNT


class GraphPreparationAdmissionOutcome(StrEnum):
    PREPARED = "prepared"
    FULL = "full"
    TARGET_CHANGED = "target_changed"


class GraphPreparationAdmissionFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("graph preparation admission failed")


async def admit_graph_preparation(
    connection: AsyncConnection,
    locks: TransitionLocks,
    binding: GraphPreparationBinding,
    selected_graph_id: str,
    capacity_limit: int,
) -> GraphPreparationAdmissionOutcome:
    if type(capacity_limit) is not int or not 0 < capacity_limit <= MAX_COUNT:
        raise ValueError("invalid graph preparation capacity limit")
    locks.require_graph_publication_scope(connection, binding, selected_graph_id)
    occupied = await locks.read_preparation_capacity(connection)
    if occupied >= capacity_limit:
        return GraphPreparationAdmissionOutcome.FULL
    target = await locks.read_graph_publication_target(
        connection, binding, selected_graph_id
    )
    if target != binding.initial_graph_id:
        return GraphPreparationAdmissionOutcome.TARGET_CHANGED

    marker = literal_column("1", Integer)
    insert_preparation = (
        insert(graph_deployment_preparation)
        .values(
            id=binding.preparation_id,
            namespace_id=binding.namespace_id,
            graph_name=binding.graph_name,
            initial_graph_id=binding.initial_graph_id,
            deployment_digest=binding.deployment_digest,
            state="open",
            result_graph_id=None,
            result_graph_version_id=None,
        )
        .returning(marker)
    )
    capacity = graph_deployment_preparation_capacity
    increment_capacity = (
        update(capacity)
        .where(capacity.c.id == 1, capacity.c.occupied == occupied)
        .values(occupied=occupied + 1)
        .returning(marker)
    )
    try:
        inserted: object = (
            await connection.execute(insert_preparation)
        ).scalar_one_or_none()
        if inserted != 1:
            raise_private_metadata_error(GraphPreparationAdmissionFailed())
        incremented: object = (
            await connection.execute(increment_capacity)
        ).scalar_one_or_none()
    except SQLAlchemyError:
        raise_private_metadata_error(GraphPreparationAdmissionFailed())
    if incremented != 1:
        raise_private_metadata_error(GraphPreparationAdmissionFailed())
    locks.require_connection(connection)
    return GraphPreparationAdmissionOutcome.PREPARED
