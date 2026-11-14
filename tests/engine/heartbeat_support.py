from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database
from kora.db.grant_history import HistoryReadLimits
from kora.engine.attempt_heartbeats import heartbeat_attempt
from kora.engine.expiry_context import ExpiryContext
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.locks import TransitionLocks
from kora.protocols.authentication import Principal
from kora.protocols.evidence import DispatcherClosure, DispatcherQuiescence
from kora.protocols.worker_claim_system import ClaimSystem
from kora.protocols.worker_heartbeats import HeartbeatResponse, WorkerHeartbeat
from tests.engine.claim_support import HISTORY_LIMITS, WORKER_BYTES, ClaimCase
from tests.engine.promotion_support import READ_LIMITS
from tests.plugins.authentication.support import AuthorityClock

HEARTBEAT_LIMITS = replace(HISTORY_LIMITS, native_bytes=200_000_000)


def heartbeat_body(granted: ClaimSystem) -> WorkerHeartbeat:
    return WorkerHeartbeat(
        lease_epoch=granted.lease_epoch,
        dispatcher_id=granted.current_grant.dispatcher_id,
    )


def self_closure(granted: ClaimSystem) -> DispatcherClosure:
    grant = granted.current_grant
    return DispatcherClosure(
        quiescence=DispatcherQuiescence(
            attempt_id=granted.attempt_id,
            execution_domain_id=grant.execution_domain_id,
            dispatcher_id=grant.dispatcher_id,
            worker_incarnation_id=grant.incarnation_id,
            lease_generation=grant.lease_epoch,
            closed_at=granted.lease_until,
            evidence_ref="registry/closure",
        ),
        provenance="registry_acknowledgement",
        process=None,
    )


def heartbeat_authority(
    case: ClaimCase,
    connection: AsyncConnection,
    locks: TransitionLocks,
    clock: AuthorityClock,
    limits: HistoryReadLimits = HEARTBEAT_LIMITS,
) -> ExpiryContext:
    parent = case.binding.run
    return ExpiryContext(
        connection,
        locks,
        GraphResourceScope(parent.namespace_id, parent.graph_id, parent.version_id),
        parent.run_id,
        clock.now,
        case.identifiers,
        READ_LIMITS,
        limits.attempt_row,
    )


async def heartbeat(
    database: Database,
    case: ClaimCase,
    clock: AuthorityClock,
    body: WorkerHeartbeat,
    principal: Principal | None = None,
    *,
    limits: HistoryReadLimits = HEARTBEAT_LIMITS,
) -> HeartbeatResponse:
    async def write(connection: AsyncConnection) -> HeartbeatResponse:
        locks = TransitionLocks(connection)
        await locks.acquire_run(case.binding.run.run_id)
        return await heartbeat_attempt(
            heartbeat_authority(case, connection, locks, clock, limits),
            case.binding.attempt_id,
            body,
            case.principal if principal is None else principal,
            limits,
            WORKER_BYTES,
        )

    return await database.run(write)
