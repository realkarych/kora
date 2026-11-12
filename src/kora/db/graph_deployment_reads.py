from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.tables import graph, graph_version

ACTIVE_VERSION = "active"


@dataclass(frozen=True, slots=True)
class StoredGraphVersion:
    version_id: str
    number: int


async def graph_id_by_name(
    connection: AsyncConnection, namespace_id: str, name: str
) -> str | None:
    found = await connection.scalar(
        select(graph.c.id).where(
            graph.c.namespace_id == namespace_id, graph.c.name == name
        )
    )
    return None if found is None else str(found)


async def active_version_by_deployment_digest(
    connection: AsyncConnection, graph_id: str, deployment_digest: str
) -> StoredGraphVersion | None:
    row = (
        await connection.execute(
            select(graph_version.c.id, graph_version.c.number)
            .where(
                graph_version.c.graph_id == graph_id,
                graph_version.c.deployment_digest == deployment_digest,
                graph_version.c.status == ACTIVE_VERSION,
            )
            .order_by(graph_version.c.number)
            .limit(1)
        )
    ).first()
    return None if row is None else StoredGraphVersion(str(row[0]), int(row[1]))


async def latest_version_number(connection: AsyncConnection, graph_id: str) -> int:
    latest = await connection.scalar(
        select(func.max(graph_version.c.number)).where(
            graph_version.c.graph_id == graph_id
        )
    )
    return 0 if latest is None else int(latest)
