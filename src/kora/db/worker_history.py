from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.grant_history import HistoryReadLimits
from kora.db.tables import attempt, node_instance, run
from kora.db.worker_claims import claim_row


async def scoped_history_attempt(
    connection: AsyncConnection,
    attempt_id: str,
    namespaces: tuple[str, ...],
    limits: HistoryReadLimits,
) -> dict[str, object] | None:
    scoped = (
        select(node_instance.c.id).join(run).where(run.c.namespace_id.in_(namespaces))
    )
    row = await claim_row(
        connection,
        attempt,
        (attempt.c.id == attempt_id) & attempt.c.node_instance_id.in_(scoped),
        (
            "node_instance_id",
            "runtime",
            "worker_id",
            "lease_owner",
            "execution_domain_id",
            "lease_epoch",
            "lease_until",
        ),
        limits.attempt_row,
    )
    return None if row is None else dict(row)
