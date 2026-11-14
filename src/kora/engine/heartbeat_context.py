from dataclasses import dataclass, replace
from datetime import datetime

from pydantic import field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.cel_eval.errors import Clock
from kora.db.grant_history import GrantHistoryRejected, HistoryReadLimits
from kora.db.tables import attempt, node_instance, run, worker, worker_incarnation
from kora.db.worker_heartbeats import (
    HEARTBEAT_VALUE_BYTES,
    heartbeat_attempt,
    heartbeat_dispatchers,
)
from kora.engine.claim_context import (
    ClaimAttempt,
    ClaimIncarnation,
    ClaimNode,
    ClaimRun,
    ClaimWorker,
    claim_record,
)
from kora.engine.expiry import observe_attempt_expiry
from kora.engine.expiry_context import (
    ACTIVE_ATTEMPT_STATUSES,
    ExpiryContext,
    ExpiryState,
)
from kora.engine.heartbeat_evidence import HeartbeatGrant, HeartbeatRecovery
from kora.engine.worker_history import HistoryAccessRejected
from kora.ir.cost import UsageCoverage
from kora.protocols.authentication import Principal
from kora.protocols.deadline_records import DeadlineEnvelope
from kora.protocols.history import GrantHistorySnapshot
from kora.protocols.identity import Count, Sha256
from kora.protocols.runtime import Liveness
from kora.protocols.worker_claim_system import GRANT_BYTES, PROCESS_BYTES
from kora.protocols.worker_heartbeats import HeartbeatAction, WorkerHeartbeat


def admit_heartbeat(
    limits: HistoryReadLimits, worker_bytes: int, authority: ExpiryContext
) -> None:
    borrowed = (
        authority.limits.parent_bytes,
        authority.limits.receipt_row_bytes,
        authority.attempt_bytes,
    )
    if any(
        type(value) is not int or not 0 < value < 2**63
        for value in (*borrowed, worker_bytes)
    ):
        raise GrantHistoryRejected()
    expiry_bytes = sum(borrowed) + 300 * 4096
    reserve = 3 * (96 * HEARTBEAT_VALUE_BYTES + 4096) + 2 * worker_bytes + expiry_bytes
    replace(
        limits,
        native_bytes=limits.native_bytes - reserve,
        raw_bytes=limits.raw_bytes
        - 3 * HEARTBEAT_VALUE_BYTES
        - 64 * 4096
        - expiry_bytes,
    ).admit(
        129, GRANT_BYTES + HEARTBEAT_VALUE_BYTES, PROCESS_BYTES + HEARTBEAT_VALUE_BYTES
    )


class HeartbeatAttempt(ClaimAttempt):
    recovery: HeartbeatRecovery | None
    counters: Liveness | None
    usage_coverage: UsageCoverage | None
    last_heartbeat_at: datetime | None
    grant_history_epoch: Count
    grant_history_count: Count
    grant_history_digest: Sha256
    verified_history_epoch: Count | None
    verified_history_count: Count | None
    verified_history_digest: Sha256 | None
    deadline_binding: DeadlineEnvelope

    @field_validator("counters", mode="before")
    @classmethod
    def no_legacy_observation(cls, value: object) -> object:
        return None if value == {} else value


@dataclass(frozen=True)
class HeartbeatContext:
    attempt: HeartbeatAttempt
    node: ClaimNode
    run: ClaimRun
    worker: ClaimWorker
    grant: HeartbeatGrant
    sealed: bool

    def history(self) -> GrantHistorySnapshot:
        return GrantHistorySnapshot(
            version=1,
            namespace_id=self.run.namespace_id,
            attempt_id=self.attempt.id,
            execution_domain_id=self.grant.execution_domain_id,
            upper_epoch=self.attempt.grant_history_epoch,
            count=self.attempt.grant_history_count,
            digest=self.attempt.grant_history_digest,
        )


def require_heartbeat_lease(
    saved: HeartbeatAttempt, body: WorkerHeartbeat, principal: Principal, now: datetime
) -> None:
    if saved.worker_id != principal.owner or saved.runtime != principal.runtime:
        raise HistoryAccessRejected("forbidden")
    if (
        saved.lease_owner != principal.owner
        or saved.lease_epoch != body.lease_epoch
        or saved.lease_until is None
        or saved.lease_until <= now
    ):
        raise HistoryAccessRejected("stale")


async def heartbeat_context(
    connection: AsyncConnection,
    attempt_id: str,
    body: WorkerHeartbeat,
    principal: Principal,
    clock: Clock,
    limits: HistoryReadLimits,
    worker_bytes: int,
    *,
    run_id: str | None = None,
) -> HeartbeatContext:
    if principal.kind != "service":
        raise HistoryAccessRejected("forbidden")
    raw = await heartbeat_attempt(
        connection,
        attempt_id,
        tuple(item.namespace_id for item in principal.namespaces),
        tuple(HeartbeatAttempt.model_fields),
        limits,
    )
    if raw is None:
        raise HistoryAccessRejected("not_found")
    saved = HeartbeatAttempt.model_validate(raw)
    require_heartbeat_lease(saved, body, principal, clock())
    node = await claim_record(
        connection,
        ClaimNode,
        node_instance,
        node_instance.c.id == saved.node_instance_id,
        limits.node_row,
    )
    if node is None or (run_id is not None and node.run_id != run_id):
        raise HistoryAccessRejected("not_found")
    parent = await claim_record(
        connection, ClaimRun, run, run.c.id == node.run_id, limits.run_row
    )
    if parent is None or parent.namespace_id not in {
        item.namespace_id for item in principal.namespaces
    }:
        raise HistoryAccessRejected("not_found")
    registered = await claim_record(
        connection,
        ClaimWorker,
        worker,
        worker.c.id == principal.owner,
        worker_bytes,
        lock=run_id is not None,
    )
    if registered is None or (registered.token_id, registered.runtime) != (
        principal.token_id,
        principal.runtime,
    ):
        raise HistoryAccessRejected("forbidden")
    rows = await heartbeat_dispatchers(
        connection,
        attempt_id,
        ((body.dispatcher_id, body.lease_epoch),),
        tuple(HeartbeatGrant.model_fields),
        limits,
    )
    if not rows:
        raise HistoryAccessRejected("stale")
    grant = HeartbeatGrant.model_validate(rows[0])
    if (grant.worker_id, grant.execution_domain_id) != (
        principal.owner,
        saved.execution_domain_id,
    ):
        raise HistoryAccessRejected("stale")
    incarnation = await claim_record(
        connection,
        ClaimIncarnation,
        worker_incarnation,
        (worker_incarnation.c.worker_id == principal.owner)
        & (worker_incarnation.c.incarnation_id == grant.incarnation_id),
        worker_bytes,
    )
    if (
        incarnation is None
        or incarnation.execution_domain_id != saved.execution_domain_id
    ):
        raise HistoryAccessRejected("stale")
    if (
        saved.grant_history_epoch,
        saved.grant_history_count,
        saved.grant_history_digest,
    ) != (grant.lease_epoch, grant.prefix_count, grant.prefix_digest):
        raise GrantHistoryRejected()
    sealed = await connection.scalar(
        select(attempt.c.execution_request.is_not(None)).where(
            attempt.c.id == attempt_id
        )
    )
    deadline = saved.deadline_binding.value
    if (None if deadline is None else deadline.deadline().at) != saved.deadline:
        raise GrantHistoryRejected()
    require_heartbeat_lease(saved, body, principal, clock())
    return HeartbeatContext(saved, node, parent, registered, grant, sealed is True)


async def heartbeat_expiry(
    authority: ExpiryContext,
    context: HeartbeatContext,
    observed_at: datetime,
    limits: HistoryReadLimits,
) -> HeartbeatContext:
    saved = context.attempt
    if (
        context.run.status != "running"
        or saved.status not in ACTIVE_ATTEMPT_STATUSES
        or context.run.deadline_expired_at is not None
        or saved.deadline_expired_at is not None
        or saved.deadline is None
        or saved.deadline > observed_at
    ):
        return context
    result = await observe_attempt_expiry(
        replace(authority, clock=lambda: observed_at), saved.id
    )
    if result is not ExpiryState.EXPIRED:
        raise GrantHistoryRejected()
    fresh = await claim_record(
        authority.connection,
        ClaimAttempt,
        attempt,
        attempt.c.id == saved.id,
        limits.attempt_row,
    )
    parent = await claim_record(
        authority.connection, ClaimRun, run, run.c.id == context.run.id, limits.run_row
    )
    if fresh is None or parent is None:
        raise GrantHistoryRejected()
    return replace(
        context, attempt=saved.model_copy(update=fresh.model_dump()), run=parent
    )


def heartbeat_actions(
    context: HeartbeatContext,
    now: datetime,
    closed: bool,
    stopped: bool,
    verified: bool,
) -> tuple[HeartbeatAction, ...]:
    saved, node, parent = context.attempt, context.node, context.run
    deadline = saved.deadline_binding.value
    expired = saved.deadline_expired_at is not None or (
        saved.deadline is not None and saved.deadline <= now
    )
    current = (
        node.current_attempt_id == saved.id
        and node.status in ("queued", "running")
        and parent.status == "running"
    )
    enclosing = (
        node.deadline_expired_at is not None or parent.deadline_expired_at is not None
    )
    active = saved.status in ("claimed", "submitted", "running")
    eligible = (
        current
        and active
        and not enclosing
        and saved.desired_state == "run"
        and saved.cancel_cause is None
        and not expired
    )
    first = context.grant.mode == "execute" and saved.grant_history_count == 1
    actions: tuple[HeartbeatAction, ...] = ("observe", "retire")
    if first and eligible and not closed and not stopped:
        actions = () if context.sealed else ("seal_request",)
    own_timeout = (
        current
        and active
        and not enclosing
        and expired
        and deadline is not None
        and deadline.scope_kind == "attempt"
        and deadline.scope_id == saved.id
        and saved.cancel_cause in (None, "timeout")
        and (saved.desired_state == "run" or saved.cancel_cause == "timeout")
    )
    if first and verified and (eligible or own_timeout):
        actions = (*actions, "report_result")
    return actions
