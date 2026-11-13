import json

import pytest
from alembic import command
from sqlalchemy import Connection, select

from kora.db.claim_sources_v1 import SQLITE_KEY
from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import attempt, attempt_claim_read_admission
from tests.db.cancel_cause_support import attempt_schema_objects
from tests.db.seed import Seeder
from tests.db.test_migrations import differences_ignoring_expression_indexes


def assert_attempt_schema_extension(
    before: tuple[str, str], after: tuple[str, str], dialect: Dialect
) -> None:
    original: list[dict[str, object]] = json.loads(before[1])
    current: list[dict[str, object]] = json.loads(after[1])
    expected = {str(index["name"]): index for index in original}
    if dialect is Dialect.SQLITE:
        expected["ix_claim_attempt_locator"] = {
            "name": "ix_claim_attempt_locator",
            "column_names": ["id"],
            "unique": 0,
            "dialect_options": {"sqlite_where": SQLITE_KEY},
        }

    assert after[0] == before[0]
    assert {str(index["name"]): index for index in current} == expected


def install_predecessor(connection: Connection, revision: str) -> None:
    command.upgrade(alembic_config(connection), revision)


def downgrade(connection: Connection) -> None:
    command.downgrade(alembic_config(connection), "0026")


@pytest.mark.asyncio
@pytest.mark.parametrize(("predecessor", "steps"), [("0025", 3), ("0026", 2)])
async def test_claim_source_migration_preserves_legacy_without_admission(
    empty_database_settings: DatabaseSettings, predecessor: str, steps: int
) -> None:
    database = Database(empty_database_settings)
    selected = tuple(
        field for field in attempt.c if field.name != "acknowledged_segment_offset"
    )
    try:
        async with database.migration_connection() as writer:
            await writer.run_sync(install_predecessor, predecessor)
        async with database.transaction() as writer:
            seeder = Seeder(writer)
            namespace_id = await seeder.namespace()
            graph_id = await seeder.graph(namespace_id)
            version_id = await seeder.graph_version(graph_id)
            run_id = await seeder.run(namespace_id, graph_id, version_id)
            node_id = await seeder.node(run_id, "legacy")
            await seeder.attempt(node_id)
            before = (await writer.execute(select(*selected))).one()
            schema_before = await writer.run_sync(attempt_schema_objects)

        report = await upgrade_database(empty_database_settings)
        async with database.read_transaction() as reader:
            after = (await reader.execute(select(*selected))).one()
            offset = await reader.scalar(select(attempt.c.acknowledged_segment_offset))
            admitted = (
                await reader.execute(select(attempt_claim_read_admission))
            ).all()
            differences = await reader.run_sync(differences_ignoring_expression_indexes)
            schema_after = await reader.run_sync(attempt_schema_objects)
        async with database.read_transaction() as reader:
            version = await reader.run_sync(current_revision)
    finally:
        await database.close()

    assert (report.from_revision, report.to_revision, report.steps) == (
        predecessor,
        "0028",
        steps,
    )
    assert after == before
    assert offset is None
    assert admitted == []
    assert differences == []
    assert version == "0028"
    if predecessor == "0026":
        assert_attempt_schema_extension(schema_before, schema_after, database.dialect)


@pytest.mark.asyncio
async def test_claim_source_revision_refuses_downgrade(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        async with database.migration_connection() as writer:
            await writer.run_sync(
                lambda connection: command.upgrade(alembic_config(connection), "0027")
            )

        async with database.migration_connection() as writer:
            with pytest.raises(
                RuntimeError, match="claim sources cannot be downgraded"
            ):
                await writer.run_sync(downgrade)

        async with database.read_transaction() as reader:
            assert await reader.run_sync(current_revision) == "0027"
    finally:
        await database.close()
