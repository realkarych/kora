import asyncio
from enum import StrEnum

from sqlalchemy import insert, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import clear_snapshot_error
from kora.db.identifiers import Clock, IdentifierGenerator, is_identifier
from kora.db.tables import event, node_instance, run
from kora.db.transactions import raise_private_metadata_error
from kora.db.types import POSTGRESQL, normalise_timestamp
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.locks import COMPONENT, Scope, ScopeKind, TransitionLocks
from kora.engine.run_materialization import (
    PreparedRunMaterialization,
    prepare_initial_node_rows,
)
from kora.engine.run_promotion_read import (
    PromotionReadLimits,
    admitted_promotion_receipt,
    read_promotion_status,
    read_saved_manual_promotion_header,
)
from kora.engine.run_queue import QueueState, observe_run_queue
from kora.engine.writer_mode import writable_postgres_transaction
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.run_promotion.prepared",
        COMPONENT,
        Level.DEBUG,
        "pending run promotion prepared for commit",
        {"outcome": FieldKind.STRING},
    )
)


class PromotionFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("the pending run promotion failed")


class PromotionOutcome(StrEnum):
    APPLIED = "applied"
    WAITING = "waiting"
    UNCHANGED = "unchanged"
    REFUSED = "refused"


def promotion_scopes(scope: GraphResourceScope) -> tuple[Scope, ...]:
    return (
        Scope(ScopeKind.NAMESPACE, (scope.namespace_id,)),
        Scope(ScopeKind.GRAPH, (scope.graph_id,)),
    )


async def _promote(
    connection: AsyncConnection,
    locks: TransitionLocks,
    scope: GraphResourceScope,
    pin: GraphVersionPin,
    materialization: PreparedRunMaterialization,
    run_id: str,
    clock: Clock,
    identifiers: IdentifierGenerator,
    limits: PromotionReadLimits,
) -> PromotionOutcome:
    locks.require_connection(connection)
    locks.require(run_id, promotion_scopes(scope))
    if not is_identifier(run_id) or not materialization.matches(scope, pin):
        return PromotionOutcome.REFUSED
    if (
        connection.dialect.name == POSTGRESQL
        and not await writable_postgres_transaction(connection)
    ):
        return PromotionOutcome.REFUSED
    receipt_id = await admitted_promotion_receipt(connection, scope, run_id, limits)
    if receipt_id is None:
        return PromotionOutcome.REFUSED
    status = await read_promotion_status(connection, scope, pin, run_id, limits)
    if status is None:
        return PromotionOutcome.REFUSED
    if status != "pending":
        return PromotionOutcome.UNCHANGED
    header = await read_saved_manual_promotion_header(
        connection, scope, pin, materialization, run_id, limits, receipt_id
    )
    if header is None:
        return PromotionOutcome.REFUSED
    if header.overlap == "queue":
        queue = await observe_run_queue(
            connection, locks, scope.namespace_id, scope.graph_id, run_id
        )
        if queue is QueueState.INVALID_ORDER:
            return PromotionOutcome.REFUSED
        if queue is not QueueState.CLEAR:
            return PromotionOutcome.WAITING
    node_ids = tuple(identifiers.new() for _ in materialization.nodes)
    event_id = identifiers.new()
    now = normalise_timestamp(clock())
    rows = prepare_initial_node_rows(materialization, scope, pin, run_id, node_ids, now)
    locks.require_connection(connection)
    if rows:
        await connection.execute(insert(node_instance), rows)
    await connection.execute(
        update(run)
        .where(run.c.id == run_id)
        .values(status="running", started_at=now, updated_at=now, needs_scheduling=True)
    )
    await connection.execute(
        insert(event).values(
            id=event_id,
            namespace_id=scope.namespace_id,
            run_id=run_id,
            node_instance_id=None,
            attempt_id=None,
            kind="run.started",
            actor={"kind": "system", "component": "scheduler"},
            payload={
                "trigger": header.model_dump(mode="json"),
                "graph_version": scope.version_id,
                "rerun_of": None,
            },
            at=now,
            created_at=now,
            updated_at=now,
        )
    )
    return PromotionOutcome.APPLIED


async def promote_pending_run(
    connection: AsyncConnection,
    locks: TransitionLocks,
    scope: GraphResourceScope,
    pin: GraphVersionPin,
    materialization: PreparedRunMaterialization,
    run_id: str,
    clock: Clock,
    identifiers: IdentifierGenerator,
    limits: PromotionReadLimits,
) -> PromotionOutcome:
    try:
        outcome = await _promote(
            connection,
            locks,
            scope,
            pin,
            materialization,
            run_id,
            clock,
            identifiers,
            limits,
        )
    except asyncio.CancelledError as error:
        clear_snapshot_error(error)
        raise
    except (SQLAlchemyError, ValueError, TypeError, RecursionError, OverflowError):
        outcome = None
    finally:
        del (
            connection,
            locks,
            scope,
            pin,
            materialization,
            run_id,
            clock,
            identifiers,
            limits,
        )
    if outcome is None:
        raise_private_metadata_error(PromotionFailed())
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED, outcome=outcome.value)
    return outcome
