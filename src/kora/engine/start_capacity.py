import enum
from dataclasses import dataclass, field
from typing import Literal, cast

from pydantic import MISSING
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.identifiers import is_identifier
from kora.db.reads import namespace_outstanding_usage
from kora.db.retention import (
    RetentionSnapshot,
    RetentionSnapshotError,
    resolve_retention_snapshot,
)
from kora.db.tables import OUTSTANDING_RUN_STATUSES, graph, namespace, run
from kora.engine.locks import COMPONENT, Scope, ScopeKind, TransitionLocks
from kora.ir.runtime import Retention
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
    RETENTION_UNAVAILABLE = "retention_unavailable"


@dataclass(frozen=True)
class StartCapacity:
    outcome: CapacityOutcome
    limit: int | None = None
    used: int | None = None
    retention: RetentionSnapshot | None = field(default=None, repr=False)


class InvalidCapacityPolicy(ValueError):
    def __init__(self) -> None:
        super().__init__("the namespace outstanding-run policy is invalid")


def capacity_result(
    outcome: CapacityOutcome,
    limit: int | None = None,
    used: int | None = None,
    *,
    retention: RetentionSnapshot | None = None,
) -> StartCapacity:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(CHECKED, outcome=outcome.value)
    return StartCapacity(outcome, limit, used, retention)


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


def resolve_manual_overlap(authored: Overlap | MISSING) -> Overlap:
    return "allow" if authored is MISSING else authored


def manual_start_scopes(namespace_id: str, graph_id: str) -> tuple[Scope, ...]:
    return (
        Scope(ScopeKind.GLOBAL),
        Scope(ScopeKind.NAMESPACE, (namespace_id,)),
        Scope(ScopeKind.GRAPH, (graph_id,)),
    )


def _retention_overrides(graph_retention: Retention | MISSING) -> dict[str, object]:
    if graph_retention is MISSING:
        return {}
    return {
        name: value
        for name, value in (
            ("transcripts", graph_retention.transcripts),
            ("artifact_payloads", graph_retention.artifact_payloads),
        )
        if value is not MISSING
    }


async def check_manual_start_capacity(
    connection: AsyncConnection,
    locks: TransitionLocks,
    principal: Principal,
    namespace_id: str,
    graph_id: str,
    run_id: str,
    overlap: Overlap,
    *,
    graph_retention: Retention | MISSING | None = None,
) -> StartCapacity:
    locks.require_connection(connection)
    if (
        principal.kind != "user"
        or not any(grant.namespace_id == namespace_id for grant in principal.namespaces)
        or not all(is_identifier(value) for value in (namespace_id, graph_id, run_id))
    ):
        return capacity_result(CapacityOutcome.REFUSED)
    locks.require(run_id, manual_start_scopes(namespace_id, graph_id))
    if type(overlap) is not str or overlap not in ("allow", "skip", "queue"):
        raise ValueError("the manual start overlap is invalid")
    overrides = (
        _retention_overrides(graph_retention)
        if graph_retention is MISSING or type(graph_retention) is Retention
        else None
    )
    invalid_overrides = graph_retention is not None and overrides is None
    del graph_retention
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
        policy: object = policy_result.scalar_one()
        limit = outstanding_limit(policy)
        namespace_retention: object = (
            MISSING
            if overrides is None
            else cast("dict[object, object]", policy).get("retention", MISSING)
        )
        del policy
    finally:
        policy_result.close()
    used = await namespace_outstanding_usage(connection, namespace_id)
    if used >= limit:
        return capacity_result(CapacityOutcome.FULL, limit, used)
    if invalid_overrides:
        return capacity_result(CapacityOutcome.RETENTION_UNAVAILABLE)
    if overrides is None:
        return capacity_result(CapacityOutcome.ROOM, limit, used)
    try:
        retention = resolve_retention_snapshot(overrides, namespace_retention)
    except RetentionSnapshotError:
        return capacity_result(CapacityOutcome.RETENTION_UNAVAILABLE)
    return capacity_result(CapacityOutcome.ROOM, limit, used, retention=retention)
