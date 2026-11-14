from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.grant_history import HistoryReadLimits
from kora.db.tables import node_instance, run, worker
from kora.db.worker_heartbeats import heartbeat_attempt as read_attempt
from kora.engine.claim_context import ClaimNode, ClaimRun, ClaimWorker, claim_record
from kora.engine.expiry_context import ExpiryContext
from kora.engine.heartbeat_context import admit_heartbeat
from kora.engine.worker_history import HistoryAccessRejected
from kora.protocols.authentication import Principal


async def heartbeat_parent(
    reader: AsyncConnection,
    att: str,
    principal: Principal,
    limits: HistoryReadLimits,
) -> ClaimRun:
    target = await read_attempt(
        reader,
        att,
        tuple(item.namespace_id for item in principal.namespaces),
        ("node_instance_id",),
        limits,
    )
    if target is None:
        raise HistoryAccessRejected("not_found")
    node = await claim_record(
        reader,
        ClaimNode,
        node_instance,
        node_instance.c.id == target["node_instance_id"],
        limits.node_row,
    )
    if node is None:
        raise HistoryAccessRejected("not_found")
    parent = await claim_record(
        reader, ClaimRun, run, run.c.id == node.run_id, limits.run_row
    )
    if parent is None or parent.namespace_id not in {
        item.namespace_id for item in principal.namespaces
    }:
        raise HistoryAccessRejected("not_found")
    return parent


async def lock_heartbeat_worker(
    authority: ExpiryContext,
    principal: Principal,
    limits: HistoryReadLimits,
    worker_bytes: int,
) -> None:
    admit_heartbeat(limits, worker_bytes, authority)
    await claim_record(
        authority.connection,
        ClaimWorker,
        worker,
        worker.c.id == principal.owner,
        worker_bytes,
        lock=True,
    )
