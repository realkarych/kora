import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import and_, case, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import (
    Database,
    DatabaseSnapshotFailure,
    DatabaseSnapshotRejected,
    DatabaseWriterFailure,
    DatabaseWriterRejected,
)
from kora.db.deadline_discovery_v1 import LEASE_ROWS
from kora.db.identifiers import (
    IDENTIFIER_LENGTH,
    Clock,
    IdentifierGenerator,
    is_identifier,
)
from kora.db.tables import approval_request, attempt, node_instance, run, wait
from kora.db.types import PATH_LENGTH, normalise_timestamp
from kora.engine.claim_context import ClaimRun, claim_record
from kora.engine.expiry import observe_attempt_expiry, record_run_expiry
from kora.engine.expiry_context import ExpiryContext, ExpiryFailed, ExpiryState
from kora.engine.expiry_reads import read_expiry_run_source
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.lease_recovery import close_undispatched_attempt, recover_expired_lease
from kora.engine.locks import Scope, ScopeDeadlineExpired, ScopeKind, TransitionLocks
from kora.engine.pending_promotion import MAX_PROMOTION_NODES
from kora.engine.run_promotion_read import PromotionReadLimits
from kora.engine.run_settlement import RunSettlementState, settle_run


async def admit_staging_inventory(connection: AsyncConnection, run_id: str) -> bool:
    node = node_instance.c
    staged_node = and_(
        node.top_level.is_(True),
        node.subgraph_attempt == 1,
        node.kind.in_(("agent", "command", "human_input")),
        func.octet_length(node.id) <= 4 * IDENTIFIER_LENGTH,
        func.octet_length(node.node_path) <= 4 * PATH_LENGTH,
    )
    nodes = select(node.id, staged_node.label("staged")).where(node.run_id == run_id)
    inventory = nodes.limit(MAX_PROMOTION_NODES + 1).cte()
    ids = select(inventory.c.id)
    attempts = select(1).where(attempt.c.node_instance_id.in_(ids))
    waits = (
        select(
            case(
                (func.octet_length(wait.c.id) <= 4 * IDENTIFIER_LENGTH, wait.c.id)
            ).label("id"),
            (wait.c.kind == "admission").label("admitted"),
        )
        .where(wait.c.node_instance_id.in_(ids), wait.c.status == "open")
        .limit(2)
        .cte()
    )
    approvals = select(1).where(approval_request.c.wait_id == waits.c.id).exists()
    unsupported_wait = select(1).where(
        or_(waits.c.id.is_(None), waits.c.admitted.is_not(True), approvals)
    )
    parent = run.c
    admitted = select(1).where(
        parent.id == run_id,
        parent.target_key.is_(None),
        parent.target_held_since.is_(None),
        ids.exists(),
        ~ids.offset(MAX_PROMOTION_NODES).exists(),
        ~ids.where(inventory.c.staged.is_not(True)).exists(),
        ~attempts.limit(1).offset(1).exists(),
        ~select(1).select_from(waits).offset(1).exists(),
        ~unsupported_wait.exists(),
    )
    return await connection.scalar(admitted) is not None


@dataclass(frozen=True, slots=True)
class StagingMaintenance:
    database: Database
    limits: PromotionReadLimits
    attempt_bytes: int
    clock: Clock
    identifiers: IdentifierGenerator
    runtime: str

    async def _parent(self, reader: AsyncConnection, run_id: str) -> ClaimRun | None:
        try:
            row = await claim_record(
                reader, ClaimRun, run, run.c.id == run_id, self.limits.parent_bytes
            )
        except (ClaimSourcesRejected, ValidationError):
            return None
        if row is not None and all(
            map(is_identifier, (row.namespace_id, row.graph_id, row.graph_version_id))
        ):
            return row
        return None

    async def _require_recovered(
        self, writer: AsyncConnection, attempt_id: str | None, startup: bool
    ) -> None:
        if (
            startup
            and await writer.scalar(
                select(attempt.c.id).where(
                    attempt.c.id == attempt_id,
                    literal_column(LEASE_ROWS),
                    attempt.c.lease_until <= normalise_timestamp(self.clock()),
                )
            )
            is not None
        ):
            raise ExpiryFailed()

    async def _apply(
        self,
        run_id: str,
        deadline: float,
        stopped: Callable[[], bool],
        *,
        attempt_id: str | None = None,
        lease: bool = False,
        startup: bool = False,
        observed_at: datetime | None = None,
    ) -> bool:
        if stopped() or not is_identifier(run_id):
            return False
        timer = asyncio.timeout_at(deadline)
        try:
            async with (
                self.database.transaction(admission_deadline=deadline) as writer,
                timer,
            ):
                locks = TransitionLocks(writer)
                if stopped():
                    return False
                if not await locks.acquire_run(run_id, wait=startup):
                    return False
                parent = await self._parent(writer, run_id)
                if parent is None:
                    await self._require_recovered(writer, attempt_id, startup)
                    return False
                scope = GraphResourceScope(
                    parent.namespace_id, parent.graph_id, parent.graph_version_id
                )
                if not lease:
                    if not await admit_staging_inventory(writer, run_id):
                        return False
                    scopes = (
                        Scope(ScopeKind.GLOBAL),
                        Scope(ScopeKind.NAMESPACE, (scope.namespace_id,)),
                        Scope(ScopeKind.GRAPH, (scope.graph_id,)),
                        Scope(ScopeKind.RUNTIME, (self.runtime,)),
                    )
                    await locks.acquire_scopes(scopes, deadline=deadline)
                if stopped():
                    return False
                context = ExpiryContext(
                    writer,
                    locks,
                    scope,
                    run_id,
                    self.clock,
                    self.identifiers,
                    self.limits,
                    self.attempt_bytes,
                )
                if attempt_id is not None:
                    if lease:
                        recovered = await recover_expired_lease(context, attempt_id)
                        if not recovered:
                            await self._require_recovered(writer, attempt_id, startup)
                        return recovered
                    return parent.deadline_expired_at is None and (
                        await observe_attempt_expiry(context, attempt_id)
                        is ExpiryState.EXPIRED
                    )
                if observed_at is not None:
                    if parent.status != "running":
                        return False
                    if parent.deadline_expired_at is None:
                        await record_run_expiry(context, observed_at)
                return await self._settle(context)
        except (ScopeDeadlineExpired, DatabaseWriterRejected, TimeoutError) as error:
            if (
                startup
                or (
                    isinstance(error, DatabaseWriterRejected)
                    and error.reason is not DatabaseWriterFailure.DEADLINE
                )
                or (isinstance(error, TimeoutError) and not timer.expired())
            ):
                raise
            return False

    async def attempt(
        self,
        attempt_id: str,
        run_id: str,
        *,
        lease: bool,
        deadline: float,
        stopped: Callable[[], bool],
        startup: bool = False,
    ) -> bool:
        return is_identifier(attempt_id) and await self._apply(
            run_id,
            deadline,
            stopped,
            attempt_id=attempt_id,
            lease=lease,
            startup=startup,
        )

    async def expire_running(
        self, run_id: str, *, deadline: float, stopped: Callable[[], bool]
    ) -> bool:
        if stopped() or not is_identifier(run_id):
            return False
        try:
            async with self.database.read_snapshot(deadline=deadline) as reader:
                parent = await self._parent(reader, run_id)
                if parent is None:
                    return False
                scope = GraphResourceScope(
                    parent.namespace_id, parent.graph_id, parent.graph_version_id
                )
                saved = await read_expiry_run_source(reader, scope, run_id, self.limits)
        except DatabaseSnapshotRejected as error:
            if error.reason is not DatabaseSnapshotFailure.DEADLINE:
                raise
            return False
        now = normalise_timestamp(self.clock())
        if saved is None or not saved.due_at(now):
            return False
        return await self._apply(run_id, deadline, stopped, observed_at=now)

    async def _settle(self, context: ExpiryContext) -> bool:
        state = await settle_run(context)
        if state is RunSettlementState.EXPIRED:
            candidate = await context.connection.scalar(
                select(attempt.c.id)
                .join(node_instance, attempt.c.node_instance_id == node_instance.c.id)
                .where(
                    node_instance.c.run_id == context.run_id,
                    attempt.c.runtime == self.runtime,
                    attempt.c.status == "created",
                    attempt.c.desired_state == "cancel",
                    func.octet_length(attempt.c.id) <= 4 * IDENTIFIER_LENGTH,
                )
            )
            if type(candidate) is str and is_identifier(candidate):
                await close_undispatched_attempt(context, candidate)
        return state in (RunSettlementState.EXPIRED, RunSettlementState.SETTLED)

    async def settle_marked(
        self, run_id: str, *, deadline: float, stopped: Callable[[], bool]
    ) -> bool:
        return await self._apply(run_id, deadline, stopped)
