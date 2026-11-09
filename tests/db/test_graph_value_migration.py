import asyncio
import json
from dataclasses import dataclass

import pytest
from alembic import command
from sqlalchemy import Connection, event, inspect, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.util import await_

from kora.db.connection import Database, DatabaseClosedError, DatabaseSettings, Dialect
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import graph_version
from tests.db.cancel_cause_support import (
    CausePredecessor,
    capture_cause_predecessor,
    migration_lock_count,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import predecessor_snapshot, seed_predecessor
from tests.db.test_migrations import differences_ignoring_expression_indexes


async def encoding_predecessor(database: Database) -> CausePredecessor:
    async with database.transaction() as connection:
        await connection.run_sync(seed_predecessor)
    async with database.migration_connection() as connection:
        await connection.run_sync(
            lambda sync: command.upgrade(alembic_config(sync), "0006")
        )
    async with database.read_transaction() as connection:
        return await connection.run_sync(capture_cause_predecessor)


def encoding_schema(connection: Connection) -> str:
    inspector = inspect(connection)
    return json.dumps(
        {
            "columns": inspector.get_columns("graph_version"),
            "checks": inspector.get_check_constraints("graph_version"),
            "indexes": inspector.get_indexes("graph_version"),
            "foreign_keys": inspector.get_foreign_keys("graph_version"),
        },
        sort_keys=True,
        default=str,
    )


@pytest.mark.asyncio
async def test_graph_version_value_encoding_migration(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        before = await encoding_predecessor(database)

        report = await upgrade_database(empty_database_settings)
        async with database.read_transaction() as connection:
            after = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.history.columns)
            )
            versions = (
                (
                    await connection.execute(
                        select(graph_version.c.value_encoding_version)
                    )
                )
                .scalars()
                .all()
            )
            columns = await connection.run_sync(
                lambda sync: inspect(sync).get_columns("graph_version")
            )
            differences = await connection.run_sync(
                differences_ignoring_expression_indexes
            )
        repeated = await upgrade_database(empty_database_settings)

        assert (report.from_revision, report.to_revision, report.steps) == (
            "0006",
            "0018",
            12,
        )
        assert after == before.history
        assert versions == [None]
        column = next(
            item for item in columns if item["name"] == "value_encoding_version"
        )
        assert column["nullable"]
        assert column.get("default") is None
        assert str(column["type"]) == "INTEGER"
        assert differences == []
        assert repeated.steps == 0
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["empty", "legacy", "encoded"])
async def test_graph_value_downgrade_always_refuses_before_mutation(
    empty_database_settings: DatabaseSettings, state: str
) -> None:
    database = Database(empty_database_settings)
    try:
        if state != "empty":
            await encoding_predecessor(database)
        async with database.migration_connection() as connection:
            await connection.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), "0007")
            )
        if state == "encoded":
            async with database.transaction() as connection:
                await connection.execute(
                    graph_version.update().values(
                        value_encoding_version=1, ir="{}", execution_contract="{}"
                    )
                )
        async with database.read_transaction() as connection:
            schema_before = await connection.run_sync(encoding_schema)
            rows_before = (await connection.execute(select(graph_version))).all()

        with pytest.raises(
            RuntimeError,
            match=(
                "^graph value history cannot be downgraded; "
                "restore a compatible backup$"
            ),
        ):
            async with database.migration_connection() as connection:
                await connection.run_sync(
                    lambda sync: command.downgrade(alembic_config(sync), "0006")
                )
        async with database.read_transaction() as connection:
            schema_after = await connection.run_sync(encoding_schema)
            rows_after = (await connection.execute(select(graph_version))).all()
            revision = await connection.run_sync(current_revision)

        assert schema_after == schema_before
        assert rows_after == rows_before
        assert revision == "0007"
        assert await migration_lock_count(database) == 0
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 2, -1])
async def test_graph_value_encoding_constraint_refuses_unsupported_versions(
    database: Database, value: int
) -> None:
    from tests.db.seed import Seeder

    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        await seeder.graph_version(graph_id)

    with pytest.raises(DBAPIError):
        async with database.transaction() as connection:
            await connection.execute(
                graph_version.update().values(value_encoding_version=value)
            )

    async with database.read_transaction() as connection:
        assert (
            await connection.execute(select(graph_version.c.value_encoding_version))
        ).scalar_one() is None


@dataclass
class EncodingUpgradeStage:
    entered: asyncio.Event
    release: asyncio.Event
    database: Database | None = None
    ledger: ConnectionLedger | None = None

    def open(self, settings: DatabaseSettings) -> Database:
        self.database = Database(settings)
        self.ledger = ConnectionLedger(self.database)
        event.listen(
            self.database.writer_engine.sync_engine, "after_cursor_execute", self.after
        )
        return self.database

    def after(
        self, connection: Connection, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement.startswith(
            "ALTER TABLE graph_version ADD COLUMN value_encoding_version"
        ):
            self.entered.set()
            await_(self.release.wait())


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "shutdown"])
async def test_graph_value_migration_recovery_releases_resources(
    empty_database_settings: DatabaseSettings, ending: str
) -> None:
    database = Database(empty_database_settings)
    stage = EncodingUpgradeStage(asyncio.Event(), asyncio.Event())
    try:
        before = await encoding_predecessor(database)
        async with database.read_transaction() as connection:
            schema_before = await connection.run_sync(encoding_schema)

        task = asyncio.create_task(
            upgrade_database(empty_database_settings, stage.open)
        )
        try:
            await asyncio.wait_for(stage.entered.wait(), 4)
            if ending == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert stage.database is not None
                await stage.database.close(deadline_seconds=0)
                stage.release.set()
                with pytest.raises(DatabaseClosedError):
                    await task
        finally:
            stage.release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            schema_after = await connection.run_sync(encoding_schema)
            after = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.history.columns)
            )
        locks = await migration_lock_count(database)
        recovered = await upgrade_database(empty_database_settings)
        async with database.read_transaction() as connection:
            differences = await connection.run_sync(
                differences_ignoring_expression_indexes
            )

        assert revision == "0006"
        assert schema_after == schema_before
        assert after == before.history
        assert locks == 0
        assert (recovered.from_revision, recovered.to_revision, recovered.steps) == (
            "0006",
            "0018",
            12,
        )
        assert differences == []
        assert stage.database is not None
        assert stage.ledger is not None
        assert stage.database.units_in_flight() == 0
        assert stage.ledger.outstanding == stage.ledger.closed_while_checked_out == 0
        assert stage.ledger.opened == stage.ledger.closed
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_graph_value_migration_failure_preserves_history_and_recovers(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    observed = EncodingUpgradeStage(asyncio.Event(), asyncio.Event())
    observed.release.set()
    try:
        before = await encoding_predecessor(database)
        async with database.transaction() as connection:
            await connection.exec_driver_sql(
                "ALTER TABLE graph_version ADD COLUMN value_encoding_version TEXT"
            )
        async with database.read_transaction() as connection:
            schema_before = await connection.run_sync(encoding_schema)

        with pytest.raises((RuntimeError, DBAPIError)):
            await upgrade_database(empty_database_settings, observed.open)
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            schema_after = await connection.run_sync(encoding_schema)
            after = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.history.columns)
            )
        locks = await migration_lock_count(database)
        async with database.transaction() as connection:
            await connection.exec_driver_sql(
                "ALTER TABLE graph_version DROP COLUMN value_encoding_version"
            )
        recovered = await upgrade_database(empty_database_settings)

        assert revision == "0006"
        assert schema_after == schema_before
        assert after == before.history
        assert locks == 0
        assert recovered.to_revision == "0018"
        assert observed.ledger is not None
        assert (
            observed.ledger.outstanding == observed.ledger.closed_while_checked_out == 0
        )
        assert observed.ledger.opened == observed.ledger.closed
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("constraint", ["absent", "unvalidated", "validated"])
async def test_graph_value_migration_handles_prepared_schema(
    empty_database_settings: DatabaseSettings, constraint: str
) -> None:
    database = Database(empty_database_settings)
    try:
        before = await encoding_predecessor(database)
        if database.dialect is Dialect.POSTGRESQL:
            async with database.transaction() as connection:
                await connection.exec_driver_sql(
                    "ALTER TABLE graph_version ADD COLUMN "
                    "value_encoding_version INTEGER"
                )
                if constraint != "absent":
                    await connection.exec_driver_sql(
                        "ALTER TABLE graph_version ADD CONSTRAINT "
                        "ck_graph_version_value_encoding_version "
                        "CHECK (value_encoding_version IS NULL "
                        "OR value_encoding_version = 1)"
                        + (" NOT VALID" if constraint == "unvalidated" else "")
                    )

        report = await upgrade_database(empty_database_settings)
        async with database.read_transaction() as connection:
            after = await connection.run_sync(
                lambda sync: predecessor_snapshot(sync, before.history.columns)
            )
            differences = await connection.run_sync(
                differences_ignoring_expression_indexes
            )

        assert report.to_revision == "0018"
        assert after == before.history
        assert differences == []
    finally:
        await database.close()
