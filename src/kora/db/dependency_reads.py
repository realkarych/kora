from dataclasses import dataclass

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.tables import node_instance

STATES = (
    "pending",
    "ready",
    "queued",
    "running",
    "waiting",
    "succeeded",
    "failed",
    "upstream_failed",
    "cancelled",
    "skipped",
)


@dataclass(frozen=True, slots=True)
class DependencyNodeState:
    status: str
    unbound: bool
    unexpired: bool


async def read_dependency_node(
    connection: AsyncConnection,
    run_id: str,
    path: str,
    kind: str,
    trigger_rule: str,
    *,
    node_id: str | None = None,
) -> DependencyNodeState | None:
    fields = node_instance.c
    statement = select(
        case((fields.status.in_(STATES), fields.status)),
        fields.current_attempt_id.is_(None),
        fields.deadline_expired_at.is_(None),
    ).where(
        fields.run_id == run_id,
        fields.node_path == path,
        fields.kind == kind,
        fields.trigger_rule == trigger_rule,
        fields.top_level.is_(True),
        fields.subgraph_attempt == 1,
    )
    if node_id is not None:
        statement = statement.where(fields.id == node_id)
    with await connection.execute(statement) as result:
        row = result.one_or_none()
    if row is None or type(row[0]) is not str:
        return None
    return DependencyNodeState(row[0], row[1], row[2])
