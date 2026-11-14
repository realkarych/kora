from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.tables import namespace
from kora.ir.agent_secrets import NamespaceSecretPolicy


class InvalidNamespacePolicy(ValueError):
    pass


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


def _flag(policy: dict[object, object], name: str) -> bool:
    value = policy.get(name, False)
    if type(value) is not bool:
        raise InvalidNamespacePolicy()
    return value


async def read_namespace_secret_policy(
    connection: AsyncConnection, namespace_id: str
) -> NamespaceSecretPolicy:
    policy: object = await connection.scalar(
        select(namespace.c.policy).where(namespace.c.id == namespace_id)
    )
    if type(policy) is not dict:
        raise InvalidNamespacePolicy()
    fields = cast("dict[object, object]", policy)
    return NamespaceSecretPolicy(
        allow_unenforced_agent_secrets=_flag(fields, "allow_unenforced_agent_secrets"),
        require_network_enforcement=_flag(fields, "require_network_enforcement"),
    )
