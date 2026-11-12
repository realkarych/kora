from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.tables import namespace


async def granted_namespace_id(
    connection: AsyncConnection, name: str, granted: tuple[str, ...]
) -> str | None:
    if not granted:
        return None
    found = await connection.scalar(
        select(namespace.c.id).where(
            namespace.c.name == name, namespace.c.id.in_(granted)
        )
    )
    return None if found is None else str(found)
