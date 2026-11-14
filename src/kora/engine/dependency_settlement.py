import asyncio
from enum import StrEnum

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from kora.db.connection import clear_snapshot_error
from kora.db.dependency_reads import read_dependency_node
from kora.db.identifiers import is_identifier
from kora.db.tables import node_instance
from kora.db.transactions import raise_private_metadata_error
from kora.db.types import POSTGRESQL, normalise_timestamp
from kora.engine.dependency_rules import (
    FINISHED,
    DependencyOutcome,
    decide_dependencies,
)
from kora.engine.expiry import observe_run_expiry
from kora.engine.expiry_context import ExpiryContext, ExpiryFailed, ExpiryState
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.locks import COMPONENT
from kora.engine.readiness import PreparedReadiness
from kora.engine.run_promotion_read import read_promotion_status
from kora.engine.writer_mode import writable_postgres_transaction
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.dependency_settlement.prepared",
        COMPONENT,
        Level.DEBUG,
        "dependency settlement prepared for commit",
        {"outcome": FieldKind.STRING},
    )
)


class DependencySettlementState(StrEnum):
    UNCHANGED = "unchanged"
    SETTLED = "settled"
    EXPIRED = "expired"
    REFUSED = "refused"


class DependencySettlementFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("dependency settlement failed")


async def _settle_dependencies(
    context: ExpiryContext,
    pin: GraphVersionPin,
    readiness: PreparedReadiness,
    node_id: str,
    node_path: str,
) -> DependencySettlementState:
    context.locks.require_connection(context.connection)
    context.require_capacity()
    if (
        not is_identifier(context.run_id)
        or not is_identifier(node_id)
        or not readiness.matches(context.scope, pin)
        or (
            context.connection.dialect.name == POSTGRESQL
            and not await writable_postgres_transaction(context.connection)
        )
    ):
        return DependencySettlementState.REFUSED
    if (
        await read_promotion_status(
            context.connection, context.scope, pin, context.run_id, context.limits
        )
        != "running"
    ):
        return DependencySettlementState.REFUSED
    nodes = {node.path: node for node in readiness.nodes}
    target = nodes.get(node_path)
    if target is None:
        return DependencySettlementState.REFUSED
    current = await read_dependency_node(
        context.connection,
        context.run_id,
        target.path,
        target.kind,
        target.trigger_rule,
        node_id=node_id,
    )
    if current is None:
        return DependencySettlementState.REFUSED
    if current.status in FINISHED:
        return DependencySettlementState.UNCHANGED
    if current.status != "pending" or not current.unbound or not current.unexpired:
        return DependencySettlementState.REFUSED
    states: list[str] = []
    for path in target.predecessors:
        predecessor = nodes[path]
        saved = await read_dependency_node(
            context.connection,
            context.run_id,
            predecessor.path,
            predecessor.kind,
            predecessor.trigger_rule,
        )
        if saved is None:
            return DependencySettlementState.REFUSED
        states.append(saved.status)
    outcome = decide_dependencies(target.trigger_rule, tuple(states))
    now = normalise_timestamp(context.clock())
    expiry = await observe_run_expiry(context, observed_at=now)
    if expiry is ExpiryState.EXPIRED:
        return DependencySettlementState.EXPIRED
    if expiry is not ExpiryState.ACTIVE or outcome is DependencyOutcome.REFUSED:
        return DependencySettlementState.REFUSED
    if outcome in (DependencyOutcome.SATISFIED, DependencyOutcome.WAITING):
        return DependencySettlementState.UNCHANGED
    await context.connection.execute(
        update(node_instance)
        .where(node_instance.c.id == node_id)
        .values(status=outcome.value, updated_at=now)
    )
    await context.event(
        f"node.{outcome.value}",
        {"node_path": target.path, "reason": "trigger_rule_unsatisfied"},
        now,
        node_id,
    )
    await context.mark(now)
    return DependencySettlementState.SETTLED


async def settle_dependencies(
    context: ExpiryContext,
    pin: GraphVersionPin,
    readiness: PreparedReadiness,
    node_id: str,
    node_path: str,
) -> DependencySettlementState:
    try:
        result = await _settle_dependencies(context, pin, readiness, node_id, node_path)
    except asyncio.CancelledError as error:
        clear_snapshot_error(error)
        raise
    except (
        SQLAlchemyError,
        ValueError,
        TypeError,
        RecursionError,
        OverflowError,
        ExpiryFailed,
    ):
        result = None
    finally:
        del context, pin, readiness, node_id, node_path
    if result is None:
        raise_private_metadata_error(DependencySettlementFailed())
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED, outcome=result.value)
    return result
