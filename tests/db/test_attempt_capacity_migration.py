import asyncio

import pytest
from alembic import command
from sqlalchemy import inspect, select
from sqlalchemy.exc import DBAPIError

from kora.db.attempt_capacity_v1 import (
    INDEXES,
    SQLITE_DEPLOYED_INDEX,
    capacity_index_dialect,
)
from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import attempt
from tests.db.cancel_cause_support import migration_lock_count
from tests.db.closing import ConnectionLedger
from tests.db.test_graph_publication_digest_migration import wait_for_invalid_index
from tests.engine.expiry_support import execution_case


@pytest.mark.asyncio
@pytest.mark.parametrize("retained_index", ["invalid", "valid"])
async def test_capacity_migration_preserves_attempts_and_recovers_indexes(
    empty_database_settings: DatabaseSettings, retained_index: str
) -> None:
    database = Database(empty_database_settings)
    ledger = ConnectionLedger(database)
    try:
        async with database.migration_connection() as migrator:
            await migrator.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), "0025")
            )
        case = await execution_case(database)
        before = await case.snapshot(database)
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "CREATE INDEX ix_attempt_physical_capacity ON attempt (id)"
            )
        with pytest.raises(
            (RuntimeError, DBAPIError), match="conflicts|already exists"
        ):
            await upgrade_database(empty_database_settings)
        async with database.transaction() as writer:
            assert await writer.run_sync(current_revision) == "0025"
            await writer.exec_driver_sql("DROP INDEX ix_attempt_physical_capacity")
        if database.dialect is Dialect.POSTGRESQL:
            if retained_index == "invalid":
                async with database.transaction() as writer:
                    await writer.exec_driver_sql("UPDATE attempt SET status = status")
                    task = asyncio.create_task(
                        upgrade_database(empty_database_settings)
                    )
                    try:
                        await wait_for_invalid_index(
                            database, "ix_attempt_physical_capacity"
                        )
                    finally:
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
            else:
                async with database.transaction() as writer:
                    for name, table, columns, _, predicate, _ in INDEXES:
                        if capacity_index_dialect(name) not in (
                            None,
                            writer.dialect.name,
                        ):
                            continue
                        suffix = "" if predicate is None else " WHERE " + predicate
                        await writer.exec_driver_sql(
                            f"CREATE INDEX {name} ON {table} "
                            f"({', '.join(columns)}){suffix}"
                        )

        report = await upgrade_database(empty_database_settings)
        repeated = await upgrade_database(empty_database_settings)
        after = await case.snapshot(database)
        async with database.read_transaction() as reader:
            with await reader.execute(select(attempt.c.id)) as result:
                ids = list(result.scalars())
            graph_indexes = await reader.run_sync(
                lambda sync: {
                    index["name"]
                    for index in inspect(sync).get_indexes("graph_version")
                }
            )

        assert (report.from_revision, report.to_revision, report.steps) == (
            "0025",
            "0026",
            1,
        )
        assert repeated.steps == 0
        assert after == before
        assert ids == [case.attempt_id]
        assert (SQLITE_DEPLOYED_INDEX in graph_indexes) is (
            database.dialect is Dialect.SQLITE
        )
        assert await migration_lock_count(database) == 0
        assert database.units_in_flight() == ledger.outstanding == 0
    finally:
        await database.close()
    assert ledger.opened == ledger.closed
