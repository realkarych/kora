import pytest
from alembic import command
from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.migrations import current_revision
from kora.db.migrations.environment import alembic_config
from kora.db.tables import graph_deployment_preparation, graph_version
from kora.db.tables import storage_address_fence as fence
from tests.db.seed import Seeder

PREDECESSOR: str = "0009"
REVISION: str = "0010"
DIGEST: str = "sha256:" + "a" * 64
OTHER_DIGEST: str = "sha256:" + "b" * 64
MAX_COUNT: int = 2**63 - 1
NATIVE_INSERT = text(
    "INSERT INTO storage_address_fence "
    "(store_id, payload_digest, last_deletion_generation, pending_deletion_generation) "
    "VALUES (:store_id, :digest, :last, :pending)"
)


async def insert_fence(
    connection: AsyncConnection, store: str, digest: str = DIGEST
) -> int:
    incarnation: object = await connection.scalar(
        fence.insert()
        .values(store_id=store, payload_digest=digest, last_deletion_generation=0)
        .returning(fence.c.fence_incarnation)
    )
    assert type(incarnation) is int
    return incarnation


def address_constraints(
    sync: Connection,
) -> tuple[list[str] | None, tuple[tuple[str, ...], ...]]:
    inspector = inspect(sync)
    primary = inspector.get_pk_constraint(fence.name)["constrained_columns"]
    unique = tuple(
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(fence.name)
    )
    return primary, unique


@pytest.mark.asyncio
async def test_fence_native_schema_uses_one_exact_address_key(
    database: Database,
) -> None:
    async with database.read_transaction() as connection:
        primary, unique = await connection.run_sync(address_constraints)
        if database.dialect is Dialect.SQLITE:
            definition: object = await connection.scalar(
                text(
                    "SELECT sql FROM sqlite_schema WHERE name = 'storage_address_fence'"
                )
            )
            collations = (
                await connection.execute(
                    text(
                        "SELECT name, coll FROM pragma_index_xinfo("
                        "(SELECT name FROM pragma_index_list('storage_address_fence') "
                        "WHERE origin = 'u')) WHERE key = 1 ORDER BY seqno"
                    )
                )
            ).all()
            sequence = None
        else:
            definition = await connection.scalar(
                text(
                    "SELECT identity_generation FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'storage_address_fence' "
                    "AND column_name = 'fence_incarnation'"
                )
            )
            collations = (
                await connection.execute(
                    text(
                        "SELECT column_name, collation_name "
                        "FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'storage_address_fence' "
                        "AND column_name IN ('store_id', 'payload_digest') "
                        "ORDER BY ordinal_position"
                    )
                )
            ).all()
            sequence = (
                await connection.execute(
                    text(
                        "SELECT s.seqtypid::regtype::text, s.seqstart, s.seqmin, "
                        "s.seqmax, s.seqincrement, s.seqcycle, c.relpersistence::text "
                        "FROM pg_sequence AS s "
                        "JOIN pg_class AS c ON c.oid = s.seqrelid "
                        "WHERE c.oid = pg_get_serial_sequence("
                        "'storage_address_fence', 'fence_incarnation')::regclass"
                    )
                )
            ).one()

    assert primary == ["fence_incarnation"]
    assert unique == (("store_id", "payload_digest"),)
    if database.dialect is Dialect.SQLITE:
        assert isinstance(definition, str)
        assert "fence_incarnation INTEGER" in definition
        assert "PRIMARY KEY AUTOINCREMENT" in definition
        assert collations == [("store_id", "BINARY"), ("payload_digest", "BINARY")]
    else:
        assert definition == "ALWAYS"
        assert sequence == ("bigint", 1, 1, MAX_COUNT, 1, False, "p")
        assert collations == [("store_id", "C"), ("payload_digest", "C")]


@pytest.mark.asyncio
async def test_fence_native_address_key_distinguishes_exact_id_and_digest(
    database: Database,
) -> None:
    widest = "!~" * 128

    async with database.transaction() as connection:
        ids = [
            await insert_fence(connection, "A"),
            await insert_fence(connection, "a"),
            await insert_fence(connection, "A", OTHER_DIGEST),
            await insert_fence(connection, widest),
        ]
    with pytest.raises(IntegrityError) as duplicate:
        async with database.transaction() as connection:
            await insert_fence(connection, "A")

    assert len(set(ids)) == 4
    assert min(ids) > 0
    assert "unique" in str(duplicate.value.orig).lower()


@pytest.mark.asyncio
async def test_fence_rejects_malformed_native_address_and_generation(
    database: Database,
) -> None:
    defaults: dict[str, str | int | float | bytes | None] = {
        "store_id": "store",
        "digest": DIGEST,
        "last": 0,
        "pending": None,
    }
    invalid = (
        ("store_id", ""),
        ("store_id", "x" * 257),
        ("store_id", "x" * 256 + " "),
        ("store_id", "store "),
        ("store_id", "store\x7f"),
        ("store_id", "stör"),
        ("store_id", "store\0"),
        ("store_id", "sto\0re"),
        ("store_id", None),
        ("digest", DIGEST + "a"),
        ("digest", DIGEST + " "),
        ("digest", DIGEST[:-1]),
        ("digest", DIGEST.replace("sha256", "SHA256")),
        ("digest", DIGEST[:-1] + "A"),
        ("digest", DIGEST + "\0"),
        ("digest", DIGEST[:-2] + "\0a"),
        ("digest", None),
        ("last", -1),
        ("last", None),
        ("pending", -1),
        ("pending", 0),
        ("pending", 1),
    )
    if database.dialect is Dialect.SQLITE:
        invalid = (
            *invalid,
            ("store_id", b"store"),
            ("digest", DIGEST.encode("ascii")),
            ("last", 1.5),
            ("last", "uncastable"),
            ("pending", 1.5),
        )

    for field, value in invalid:
        with pytest.raises(DBAPIError):
            async with database.transaction() as connection:
                await connection.execute(NATIVE_INSERT, {**defaults, field: value})
    async with database.transaction() as connection:
        await connection.execute(
            NATIVE_INSERT, {**defaults, "last": MAX_COUNT, "pending": MAX_COUNT}
        )
        stored = (
            await connection.execute(
                select(
                    fence.c.last_deletion_generation,
                    fence.c.pending_deletion_generation,
                )
            )
        ).all()

    assert stored == [(MAX_COUNT, MAX_COUNT)]


@pytest.mark.asyncio
async def test_fence_migration_preserves_history_and_committed_incarnation(
    empty_database_settings: DatabaseSettings,
) -> None:
    before = Database(empty_database_settings)
    try:
        async with before.migration_connection() as connection:
            await connection.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), PREDECESSOR)
            )
        async with before.transaction() as connection:
            seeder = Seeder(connection)
            namespace_id = await seeder.namespace()
            graph_id = await seeder.graph(namespace_id)
            version_id = await seeder.graph_version(graph_id)
            preparation = {
                "id": seeder.new_id(),
                "namespace_id": namespace_id,
                "graph_name": "pipeline",
                "initial_graph_id": graph_id,
                "deployment_digest": "a" * 64,
                "state": "published",
                "result_graph_id": graph_id,
                "result_graph_version_id": version_id,
            }
            await connection.execute(
                graph_deployment_preparation.insert().values(**preparation)
            )
            original = dict(
                (await connection.execute(select(graph_version))).mappings().one()
            )
    finally:
        await before.close()

    first = Database(empty_database_settings)
    try:
        async with first.migration_connection() as connection:
            await connection.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), REVISION)
            )
        async with first.transaction() as connection:
            old = await insert_fence(connection, "store")
        async with first.transaction() as connection:
            await connection.execute(
                fence.delete().where(fence.c.fence_incarnation == old)
            )
        async with first.migration_connection() as connection:
            await connection.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), REVISION)
            )
        with pytest.raises(
            RuntimeError, match="storage address fence history cannot be downgraded"
        ):
            async with first.migration_connection() as connection:
                await connection.run_sync(
                    lambda sync: command.downgrade(alembic_config(sync), PREDECESSOR)
                )
    finally:
        await first.close()
    reopened = Database(empty_database_settings)
    try:
        async with reopened.transaction() as connection:
            revision = await connection.run_sync(current_revision)
            new = await insert_fence(connection, "store")
            version_after = dict(
                (await connection.execute(select(graph_version))).mappings().one()
            )
            preparation_after = dict(
                (await connection.execute(select(graph_deployment_preparation)))
                .mappings()
                .one()
            )
    finally:
        await reopened.close()

    assert revision == REVISION
    assert new > old > 0
    assert version_after == original
    assert preparation_after == preparation
