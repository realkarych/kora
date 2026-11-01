import enum
from dataclasses import dataclass
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.identifiers import is_identifier
from kora.db.reads import namespace_outstanding_usage
from kora.db.tables import OUTSTANDING_RUN_STATUSES, graph, namespace, run
from kora.engine.locks import COMPONENT, Scope, ScopeKind, TransitionLocks
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.authentication import Principal

Overlap = Literal["allow", "skip", "queue"]
DEFAULT_MAX_PENDING_RUNS: int = 1000
CHECKED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.start_capacity.checked",
        COMPONENT,
        Level.DEBUG,
        "manual start capacity checked",
        {"outcome": FieldKind.STRING},
    )
)


class CapacityOutcome(enum.StrEnum):
    ROOM = "room"
    FULL = "full"
    SKIPPED = "skipped"
    REFUSED = "refused"


@dataclass(frozen=True)
class StartCapacity:
    outcome: CapacityOutcome
    limit: int | None = None
    used: int | None = None


class InvalidCapacityPolicy(ValueError):
    def __init__(self) -> None:
        super().__init__("the namespace outstanding-run policy is invalid")


def capacity_result(
    outcome: CapacityOutcome, limit: int | None = None, used: int | None = None
) -> StartCapacity:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(CHECKED, outcome=outcome.value)
    return StartCapacity(outcome, limit, used)


def outstanding_limit(policy: object) -> int:
    if type(policy) is not dict:
        raise InvalidCapacityPolicy()
    fields = cast("dict[object, object]", policy)
    if "concurrency" not in fields:
        return DEFAULT_MAX_PENDING_RUNS
    concurrency = fields["concurrency"]
    if type(concurrency) is not dict:
        raise InvalidCapacityPolicy()
    limits = cast("dict[object, object]", concurrency)
    if "max_pending_runs" not in limits:
        return DEFAULT_MAX_PENDING_RUNS
    limit = limits["max_pending_runs"]
    if type(limit) is not int or limit <= 0:
        raise InvalidCapacityPolicy()
    return limit


async def check_manual_start_capacity(
    connection: AsyncConnection,
    locks: TransitionLocks,
    principal: Principal,
    namespace_id: str,
    graph_id: str,
    run_id: str,
    overlap: Overlap,
) -> StartCapacity:
    locks.require_connection(connection)
    if (
        principal.kind != "user"
        or not any(grant.namespace_id == namespace_id for grant in principal.namespaces)
        or not all(is_identifier(value) for value in (namespace_id, graph_id, run_id))
    ):
        return capacity_result(CapacityOutcome.REFUSED)
    locks.require(
        run_id,
        (
            Scope(ScopeKind.GLOBAL),
            Scope(ScopeKind.NAMESPACE, (namespace_id,)),
            Scope(ScopeKind.GRAPH, (graph_id,)),
        ),
    )
    if type(overlap) is not str or overlap not in ("allow", "skip", "queue"):
        raise ValueError("the manual start overlap is invalid")
    graph_result = await connection.execute(
        select(graph.c.id).where(
            graph.c.id == graph_id, graph.c.namespace_id == namespace_id
        )
    )
    try:
        present = graph_result.scalar_one_or_none() is not None
    finally:
        graph_result.close()
    if not present:
        return capacity_result(CapacityOutcome.REFUSED)
    if overlap == "skip":
        overlap_result = await connection.execute(
            select(
                select(run.c.id)
                .where(
                    run.c.namespace_id == namespace_id,
                    run.c.graph_id == graph_id,
                    run.c.status.in_(OUTSTANDING_RUN_STATUSES),
                )
                .exists()
            )
        )
        try:
            overlapping = overlap_result.scalar_one()
        finally:
            overlap_result.close()
        if overlapping:
            return capacity_result(CapacityOutcome.SKIPPED)
    policy_result = await connection.execute(
        select(namespace.c.policy).where(namespace.c.id == namespace_id)
    )
    try:
        limit = outstanding_limit(policy_result.scalar_one())
    finally:
        policy_result.close()
    used = await namespace_outstanding_usage(connection, namespace_id)
    outcome = CapacityOutcome.FULL if used >= limit else CapacityOutcome.ROOM
    return capacity_result(outcome, limit, used)
