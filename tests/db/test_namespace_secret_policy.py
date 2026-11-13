import pytest
from sqlalchemy import update

from kora.db.connection import Database
from kora.db.namespace_reads import (
    InvalidNamespacePolicy,
    read_namespace_secret_policy,
)
from kora.db.tables import namespace
from kora.ir.agent_secrets import NamespaceSecretPolicy
from tests.db.seed import Seeder


async def saved(database: Database, policy: object) -> str:
    async with database.transaction() as writer:
        namespace_id = await Seeder(writer).namespace("example")
        await writer.execute(
            update(namespace)
            .where(namespace.c.id == namespace_id)
            .values(policy=policy)
        )
    return namespace_id


async def read(database: Database, namespace_id: str) -> NamespaceSecretPolicy:
    async with database.read_transaction() as reader:
        return await read_namespace_secret_policy(reader, namespace_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ({"concurrency": {"max_pending_runs": 5}}, (False, False)),
        ({"allow_unenforced_agent_secrets": True}, (True, False)),
        ({"require_network_enforcement": True}, (False, True)),
        (
            {
                "allow_unenforced_agent_secrets": False,
                "require_network_enforcement": False,
            },
            (False, False),
        ),
    ],
)
async def test_namespace_secret_policy_reads_saved_flags(
    database: Database, policy: object, expected: tuple[bool, bool]
) -> None:
    namespace_id = await saved(database, policy)

    result = await read(database, namespace_id)

    assert (
        result.allow_unenforced_agent_secrets,
        result.require_network_enforcement,
    ) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [
        {"allow_unenforced_agent_secrets": 1},
        {"allow_unenforced_agent_secrets": "true"},
        {"allow_unenforced_agent_secrets": None},
        {"require_network_enforcement": 0},
        [],
    ],
)
async def test_namespace_secret_policy_refuses_invalid_values(
    database: Database, policy: object
) -> None:
    namespace_id = await saved(database, policy)

    with pytest.raises(InvalidNamespacePolicy):
        await read(database, namespace_id)


@pytest.mark.asyncio
async def test_namespace_secret_policy_refuses_a_missing_namespace(
    database: Database,
) -> None:
    namespace_id = await saved(database, {})

    with pytest.raises(InvalidNamespacePolicy):
        await read(database, namespace_id + "0")
