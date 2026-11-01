import json
from dataclasses import dataclass

import pytest
from alembic import command
from sqlalchemy import Connection, MetaData, Table, insert, inspect, select
from sqlalchemy.exc import DBAPIError

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.reads import retention_snapshot_by_run_id, run_by_id
from kora.db.retention import UnversionedRetention
from kora.db.types import JsonValue
from tests.db.seed import PredecessorHistory, predecessor_snapshot, seed_predecessor
from tests.db.test_migrations import differences_ignoring_expression_indexes


@dataclass(frozen=True)
class RetentionPredecessor:
    history: PredecessorHistory
    run_table: Table
    old_insert: dict[str, object]
    foreign_keys: tuple[str, ...]


def run_foreign_keys(connection: Connection) -> tuple[str, ...]:
    return tuple(
        sorted(str(value) for value in inspect(connection).get_foreign_keys("run"))
    )


def seed_retention_rows(
    connection: Connection, original: PredecessorHistory
) -> RetentionPredecessor:
    run_table = Table("run", MetaData(), autoload_with=connection, resolve_fks=False)
    first: dict[str, object] = dict(
        connection.execute(select(run_table)).mappings().one()
    )
    legacy_values: tuple[JsonValue, ...] = (
        {},
        {"transcripts": "0007d"},
        {"transcripts": "3d", "artifact_payloads": "forever"},
    )
    for index, retention in enumerate(legacy_values):
        values = {
            **first,
            "id": f"legacy-{index}",
            "idempotency_key": f"legacy-{index}",
            "retention": json.dumps(retention, indent=2)
            if connection.dialect.name == "sqlite"
            else retention,
        }
        connection.execute(insert(run_table).values(**values))
    old_insert: dict[str, object] = {
        **first,
        "id": "old-writer-after-upgrade",
        "idempotency_key": "old-writer-after-upgrade",
        "retention": '{ "legacy" : [0, "untouched"] }'
        if connection.dialect.name == "sqlite"
        else {"legacy": [0, "untouched"]},
    }
    return RetentionPredecessor(
        predecessor_snapshot(connection, original.columns),
        run_table,
        old_insert,
        run_foreign_keys(connection),
    )


async def seed_retention_predecessor(database: Database) -> RetentionPredecessor:
    async with database.transaction() as connection:
        original = await connection.run_sync(seed_predecessor)
    async with database.migration_connection() as connection:
        await connection.run_sync(
            lambda sync: command.upgrade(alembic_config(sync), "0002")
        )
    async with database.transaction() as connection:
        return await connection.run_sync(
            lambda sync: seed_retention_rows(sync, original)
        )


@pytest.mark.asyncio
async def test_retention_snapshot_migration_preserves_legacy_rows(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        before = await seed_retention_predecessor(database)
        await database.close()

        report = await upgrade_database(empty_database_settings)

        database = Database(empty_database_settings)
        async with database.read_transaction() as connection:
            after = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.history.columns)
            )
            revision = await connection.run_sync(current_revision)
            differences = await connection.run_sync(
                differences_ignoring_expression_indexes
            )
            foreign_keys = await connection.run_sync(run_foreign_keys)
            columns = await connection.run_sync(
                lambda sync: inspect(sync).get_columns("run")
            )
            checks = await connection.run_sync(
                lambda sync: inspect(sync).get_check_constraints("run")
            )
            classifications = [
                await retention_snapshot_by_run_id(connection, "old-namespace", run_id)
                for run_id in ("old-run", "legacy-0", "legacy-1", "legacy-2")
            ]
        async with database.transaction() as connection:
            await connection.execute(
                insert(before.run_table).values(**before.old_insert)
            )
        async with database.read_transaction() as connection:
            inserted = dict(
                (
                    await connection.execute(
                        select(before.run_table).where(
                            before.run_table.c.id == before.old_insert["id"]
                        )
                    )
                )
                .mappings()
                .one()
            )
            old_writer_snapshot = await retention_snapshot_by_run_id(
                connection, "old-namespace", str(before.old_insert["id"])
            )
            old_writer_row = await run_by_id(
                connection, "old-namespace", str(before.old_insert["id"])
            )
        retry = await upgrade_database(empty_database_settings)

        assert (report.from_revision, report.to_revision, report.steps) == (
            "0002",
            "0005",
            3,
        )
        assert revision == "0005"
        assert after == before.history
        assert differences == []
        assert foreign_keys == before.foreign_keys
        retention_column = next(
            column for column in columns if column["name"] == "retention_version"
        )
        assert retention_column["nullable"]
        assert retention_column.get("default") is None
        assert str(retention_column["type"]) == "INTEGER"
        assert any(check["name"] == "ck_run_retention_version" for check in checks)
        assert classifications == [UnversionedRetention()] * 4
        assert "retention_version" not in before.old_insert
        assert inserted == before.old_insert
        assert old_writer_snapshot == UnversionedRetention()
        assert old_writer_row is not None
        assert old_writer_row.retention_version is None
        assert old_writer_row.retention == {"legacy": [0, "untouched"]}
        assert retry.steps == 0
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_constraint", [False, True])
async def test_retention_snapshot_migration_recovers_compatible_schema(
    empty_database_settings: DatabaseSettings, with_constraint: bool
) -> None:
    database = Database(empty_database_settings)
    try:
        before = await seed_retention_predecessor(database)
        async with database.transaction() as connection:
            if database.dialect is Dialect.POSTGRESQL:
                await connection.exec_driver_sql(
                    "ALTER TABLE run ADD COLUMN retention_version INTEGER"
                )
                if with_constraint:
                    await connection.exec_driver_sql(
                        "ALTER TABLE run ADD CONSTRAINT ck_run_retention_version "
                        "CHECK (retention_version IS NULL OR retention_version = 1) "
                        "NOT VALID"
                    )
        await database.close()

        report = await upgrade_database(empty_database_settings)

        database = Database(empty_database_settings)
        async with database.read_transaction() as connection:
            after = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.history.columns)
            )
            differences = await connection.run_sync(
                differences_ignoring_expression_indexes
            )
            snapshot = await retention_snapshot_by_run_id(
                connection, "old-namespace", "old-run"
            )
            if database.dialect is Dialect.POSTGRESQL:
                validated = (
                    await connection.exec_driver_sql(
                        "SELECT convalidated FROM pg_constraint "
                        "WHERE conname = 'ck_run_retention_version' "
                        "AND conrelid = 'run'::regclass"
                    )
                ).scalar_one()
                assert validated is True

        assert report.to_revision == "0005"
        assert report.steps == 3
        assert after == before.history
        assert differences == []
        assert snapshot == UnversionedRetention()
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "definition",
    [
        "TEXT",
        "INTEGER DEFAULT 1",
        "INTEGER NOT NULL DEFAULT 1",
        "INTEGER CONSTRAINT ck_run_retention_version CHECK (retention_version = 2)",
    ],
)
async def test_retention_snapshot_migration_refuses_incompatible_schema(
    empty_database_settings: DatabaseSettings, definition: str
) -> None:
    database = Database(empty_database_settings)
    try:
        before = await seed_retention_predecessor(database)
        async with database.transaction() as connection:
            await connection.exec_driver_sql(
                f"ALTER TABLE run ADD COLUMN retention_version {definition}"
            )
        await database.close()
        error_type = DBAPIError if database.dialect is Dialect.SQLITE else RuntimeError
        error_pattern = (
            "duplicate column name"
            if database.dialect is Dialect.SQLITE
            else "retention migration found an incompatible schema object"
        )

        with pytest.raises(error_type, match=error_pattern):
            await upgrade_database(empty_database_settings)

        database = Database(empty_database_settings)
        async with database.read_transaction() as connection:
            after = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.history.columns)
            )
            revision = await connection.run_sync(current_revision)

        assert revision == "0002"
        assert after == before.history
    finally:
        await database.close()
