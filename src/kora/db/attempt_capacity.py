from dataclasses import dataclass

from sqlalchemy import String, case, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.tables import (
    NON_TERMINAL_ATTEMPT_STATUSES,
    attempt,
    graph_version,
    node_instance,
    run,
)

RECOVERY_SAMPLE_SIZE: int = 20


@dataclass(frozen=True, slots=True)
class AttemptCapacity:
    global_attempts: int
    namespace_attempts: int
    graph_attempts: int
    runtime_attempts: int
    graph_runs: int
    run_attempts: int


@dataclass(frozen=True, slots=True)
class RunRecovery:
    count: int
    attempt_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GraphCapSource:
    version_id: str
    number: int


async def read_attempt_capacity(
    connection: AsyncConnection,
    *,
    namespace_id: str,
    graph_id: str,
    run_id: str,
    runtime: str,
) -> AttemptCapacity:
    owner = (
        select(node_instance.c.run_id)
        .where(node_instance.c.id == attempt.c.node_instance_id)
        .correlate(attempt)
        .limit(1)
        .scalar_subquery()
    )
    owner_namespace = (
        select(run.c.namespace_id)
        .where(run.c.id == owner)
        .correlate(attempt)
        .limit(1)
        .scalar_subquery()
    )
    owner_graph = (
        select(run.c.graph_id)
        .where(run.c.id == owner)
        .correlate(attempt)
        .limit(1)
        .scalar_subquery()
    )
    statement = (
        select(
            func.count(),
            func.count(case((owner_namespace == namespace_id, 1))),
            func.count(case((owner_graph == graph_id, 1))),
            func.count(case((attempt.c.runtime == runtime, 1))),
            func.count(func.distinct(case((owner_graph == graph_id, owner)))),
            func.count(case((owner == run_id, 1))),
        )
        .select_from(attempt)
        .where(
            attempt.c.status.in_(
                tuple(
                    literal_column(f"'{status}'", String())
                    for status in NON_TERMINAL_ATTEMPT_STATUSES
                )
            )
        )
    )
    with await connection.execute(statement) as result:
        return AttemptCapacity(*result.one())


async def read_run_recovery(connection: AsyncConnection, run_id: str) -> RunRecovery:
    statement = (
        select(attempt.c.id, func.count().over())
        .join(node_instance, attempt.c.node_instance_id == node_instance.c.id)
        .where(node_instance.c.run_id == run_id, attempt.c.recovery_required.is_(True))
        .order_by(attempt.c.id)
        .limit(RECOVERY_SAMPLE_SIZE)
    )
    with await connection.execute(statement) as result:
        rows = result.all()
    return RunRecovery(
        int(rows[0][1]) if rows else 0, tuple(str(row[0]) for row in rows)
    )


async def read_graph_cap_source(
    connection: AsyncConnection, graph_id: str
) -> GraphCapSource | None:
    candidates = (
        select(graph_version.c.id, graph_version.c.number)
        .where(graph_version.c.graph_id == graph_id)
        .order_by(graph_version.c.number.desc())
        .limit(1)
    )
    with await connection.execute(
        candidates.where(graph_version.c.status == literal_column("'active'", String()))
    ) as result:
        row = result.one_or_none()
    if row is None:
        with await connection.execute(candidates) as result:
            row = result.one_or_none()
    return None if row is None else GraphCapSource(*row)
