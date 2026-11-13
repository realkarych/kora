import pytest
from alembic import command
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from kora.db.connection import Database, DatabaseSettings
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import attempt, worker
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_migrations import differences_ignoring_expression_indexes
from tests.engine.test_grant_history import seed


@pytest.mark.asyncio
async def test_claim_poll_migration_preserves_workers_and_recovers(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    ledger = ConnectionLedger(database)
    old_worker = tuple(
        field
        for field in worker.c
        if field.name not in ("claim_cursor", "claim_cursor_generation")
    )
    try:
        async with database.migration_connection() as migrator:
            await migrator.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), "0028")
            )
        await seed(database)
        async with database.read_transaction() as reader:
            original_worker = (await reader.execute(select(*old_worker))).all()
            original_attempt = (await reader.execute(select(attempt))).all()
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "CREATE INDEX ix_attempt_claim_candidate ON attempt (id)"
            )
        with pytest.raises(
            (RuntimeError, DBAPIError), match="conflicts|already exists"
        ):
            await upgrade_database(empty_database_settings)
        async with database.transaction() as writer:
            assert await writer.run_sync(current_revision) == "0028"
            await writer.exec_driver_sql("DROP INDEX ix_attempt_claim_candidate")

        report = await upgrade_database(empty_database_settings)
        replay = await upgrade_database(empty_database_settings)
        async with database.read_transaction() as reader:
            saved_worker = (await reader.execute(select(*old_worker))).all()
            saved_attempt = (await reader.execute(select(attempt))).all()
            cursor = (
                await reader.execute(
                    select(worker.c.claim_cursor, worker.c.claim_cursor_generation)
                )
            ).one()
            differences = await reader.run_sync(differences_ignoring_expression_indexes)

        assert (report.from_revision, report.to_revision, report.steps) == (
            "0028",
            "0029",
            1,
        )
        assert replay.steps == 0
        assert original_worker == saved_worker
        assert original_attempt == saved_attempt
        assert tuple(cursor) == (None, 0)
        assert differences == []
        assert database.units_in_flight() == ledger.outstanding == 0
    finally:
        await database.close()
    assert ledger.opened == ledger.closed


@pytest.mark.asyncio
async def test_claim_cursor_default_covers_old_registration_after_upgrade(
    database: Database,
) -> None:
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        token_id = await seeder.token("d" * 64, kind="service")

    async with database.transaction() as writer:
        await Seeder(writer).worker(token_id)
    async with database.read_transaction() as reader:
        cursor = (
            await reader.execute(
                select(worker.c.claim_cursor, worker.c.claim_cursor_generation)
            )
        ).one()

    assert tuple(cursor) == (None, 0)
