import asyncio

import pytest
from alembic import command
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import run, run_promotion_admission
from tests.db.cancel_cause_support import migration_lock_count
from tests.db.closing import ConnectionLedger
from tests.db.test_graph_publication_digest_migration import wait_for_invalid_index
from tests.engine.test_run_queue import queue_case


@pytest.mark.asyncio
@pytest.mark.parametrize("retained_index", ["invalid", "valid"])
async def test_pending_discovery_migration_recovers_without_backfill(
    empty_database_settings: DatabaseSettings, retained_index: str
) -> None:
    database = Database(empty_database_settings)
    ledger = ConnectionLedger(database)
    try:
        async with database.migration_connection() as migrator:
            await migrator.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), "0021")
            )
        await queue_case(database)
        async with database.read_transaction() as reader:
            with await reader.execute(select(run).order_by(run.c.id)) as rows:
                before = [dict(row) for row in rows.mappings()]
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "CREATE INDEX ix_run_status_id ON run (status)"
            )
        with pytest.raises(
            (RuntimeError, DBAPIError), match="conflicts|already exists"
        ):
            await upgrade_database(empty_database_settings)
        async with database.transaction() as writer:
            assert await writer.run_sync(current_revision) == "0021"
            await writer.exec_driver_sql("DROP INDEX ix_run_status_id")
        if database.dialect is Dialect.POSTGRESQL:
            if retained_index == "invalid":
                async with database.transaction() as writer:
                    await writer.exec_driver_sql("UPDATE run SET status = status")
                    task = asyncio.create_task(
                        upgrade_database(empty_database_settings)
                    )
                    try:
                        await wait_for_invalid_index(database, "ix_run_status_id")
                    finally:
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
            else:
                async with database.transaction() as writer:
                    await writer.exec_driver_sql(
                        "CREATE INDEX ix_run_status_id ON run (status, id)"
                    )

        report = await upgrade_database(empty_database_settings)
        repeated = await upgrade_database(empty_database_settings)
        async with database.read_transaction() as reader:
            with await reader.execute(select(run).order_by(run.c.id)) as rows:
                after = [dict(row) for row in rows.mappings()]
            admissions = await reader.scalar(select(run_promotion_admission.c.run_id))

        assert (report.from_revision, report.to_revision, report.steps) == (
            "0021",
            "0023",
            2,
        )
        assert repeated.steps == 0
        assert after == before
        assert admissions is None
        assert await migration_lock_count(database) == 0
        assert database.units_in_flight() == ledger.outstanding == 0
    finally:
        await database.close()
    assert ledger.opened == ledger.closed
