import enum
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.identifiers import Clock, IdentifierGenerator, is_identifier
from kora.db.tables import event, graph, graph_version, node_instance, run
from kora.db.types import as_json_value, normalise_timestamp
from kora.engine.locks import COMPONENT, Scope, ScopeKind, TransitionLocks
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.authentication import Principal
from kora.protocols.identity_records import Identifier

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.pending_cancellation.prepared",
        COMPONENT,
        Level.DEBUG,
        "pending cancellation prepared for commit",
        {"wake_required": FieldKind.BOOLEAN},
    )
)


class CancellationOutcome(enum.StrEnum):
    APPLIED = "applied"
    UNCHANGED = "unchanged"
    REFUSED = "refused"


@dataclass(frozen=True)
class CancellationResult:
    outcome: CancellationOutcome
    wake_required: bool = False


class PendingRunState(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    graph_version_id: Identifier
    rerun_of_run_id: Identifier | None
    status: str
    needs_scheduling: bool
    started_at: datetime | None
    deadline_expired_at: datetime | None
    finished_at: datetime | None
    target_held_since: datetime | None
    has_nodes: bool

    def outcome(self) -> CancellationOutcome:
        if (
            self.has_nodes
            or self.started_at is not None
            or self.deadline_expired_at is not None
            or self.target_held_since is not None
        ):
            return CancellationOutcome.REFUSED
        if self.status == "pending" and self.finished_at is None:
            return CancellationOutcome.APPLIED
        if self.status == "cancelled" and self.finished_at is not None:
            return CancellationOutcome.UNCHANGED
        return CancellationOutcome.REFUSED


async def read_pending_state(
    connection: AsyncConnection, namespace_id: str, graph_id: str, run_id: str
) -> PendingRunState | None:
    has_nodes = (
        select(node_instance.c.id).where(node_instance.c.run_id == run_id).exists()
    )
    statement = (
        select(
            run.c.graph_version_id,
            run.c.rerun_of_run_id,
            run.c.status,
            run.c.needs_scheduling,
            run.c.started_at,
            run.c.deadline_expired_at,
            run.c.finished_at,
            run.c.target_held_since,
            has_nodes.label("has_nodes"),
        )
        .select_from(
            run.join(
                graph,
                (run.c.graph_id == graph.c.id)
                & (run.c.namespace_id == graph.c.namespace_id),
            ).join(
                graph_version,
                (run.c.graph_version_id == graph_version.c.id)
                & (graph_version.c.graph_id == graph.c.id),
            )
        )
        .where(
            run.c.id == run_id,
            run.c.namespace_id == namespace_id,
            run.c.graph_id == graph_id,
        )
    )
    result = await connection.execute(statement)
    try:
        row = result.mappings().one_or_none()
        return None if row is None else PendingRunState.model_validate(dict(row))
    finally:
        result.close()


async def cancel_pending_run(
    connection: AsyncConnection,
    locks: TransitionLocks,
    principal: Principal,
    namespace_id: str,
    graph_id: str,
    run_id: str,
    clock: Clock,
    identifiers: IdentifierGenerator,
) -> CancellationResult:
    locks.require_connection(connection)
    if (
        principal.kind != "user"
        or not any(grant.namespace_id == namespace_id for grant in principal.namespaces)
        or not all(is_identifier(value) for value in (namespace_id, graph_id, run_id))
    ):
        return CancellationResult(CancellationOutcome.REFUSED)
    locks.require(
        run_id,
        (
            Scope(ScopeKind.GLOBAL),
            Scope(ScopeKind.NAMESPACE, (namespace_id,)),
            Scope(ScopeKind.GRAPH, (graph_id,)),
        ),
    )
    state = await read_pending_state(connection, namespace_id, graph_id, run_id)
    if state is None:
        return CancellationResult(CancellationOutcome.REFUSED)
    outcome = state.outcome()
    if outcome is not CancellationOutcome.APPLIED:
        return CancellationResult(outcome)
    trigger_result = await connection.execute(
        select(run.c.trigger).where(run.c.id == run_id)
    )
    try:
        trigger = as_json_value(trigger_result.scalar_one())
    finally:
        trigger_result.close()
    if not isinstance(trigger, dict):
        raise ValueError("the saved run trigger must be an object")
    now = normalise_timestamp(clock())
    event_id = identifiers.new()
    await connection.execute(
        update(run)
        .where(run.c.id == run_id)
        .values(
            status="cancelled",
            finished_at=now,
            updated_at=now,
            needs_scheduling=True,
        )
    )
    await connection.execute(
        insert(event).values(
            id=event_id,
            namespace_id=namespace_id,
            run_id=run_id,
            node_instance_id=None,
            attempt_id=None,
            kind="run.cancelled",
            actor={
                "kind": "user",
                "owner": principal.owner,
                "token_id": principal.token_id,
            },
            payload={
                "trigger": trigger,
                "graph_version": state.graph_version_id,
                "rerun_of": state.rerun_of_run_id,
            },
            at=now,
            created_at=now,
            updated_at=now,
        )
    )
    wake_required = not state.needs_scheduling
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED, wake_required=wake_required)
    return CancellationResult(CancellationOutcome.APPLIED, wake_required)
