import pytest
from alembic import command
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from kora.db.connection import Database, DatabaseSettings
from kora.db.migrations import current_revision
from kora.db.migrations.environment import alembic_config
from kora.db.tables import attempt, attempt_dispatcher
from tests.db.closing import ConnectionLedger
from tests.db.predecessor import (
    historical_schema_differences,
    upgrade_to_run_event_metadata,
)
from tests.engine.test_grant_history import legacy, seed


@pytest.mark.asyncio
async def test_worker_claim_migration_preserves_legacy_grants_and_recovers(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    ledger = ConnectionLedger(database)
    old_attempt = tuple(
        field for field in attempt.c if field.name != "current_dispatcher_open"
    )
    old_dispatcher = tuple(
        field
        for field in attempt_dispatcher.c
        if field.name not in ("claim_id", "claim_digest")
    )
    try:
        async with database.migration_connection(target="0030") as migrator:
            await migrator.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), "0030")
            )
        scope = await seed(database)
        await legacy(database, scope, range(1, 3))
        async with database.read_transaction() as reader:
            before_attempt = (await reader.execute(select(*old_attempt))).all()
            before_dispatchers = (
                await reader.execute(
                    select(*old_dispatcher).order_by(attempt_dispatcher.c.id)
                )
            ).all()
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "CREATE INDEX ix_attempt_worker_open ON attempt (id)"
            )
        with pytest.raises(
            (RuntimeError, DBAPIError), match="conflicts|already exists"
        ):
            await upgrade_to_run_event_metadata(empty_database_settings)
        async with database.transaction() as writer:
            assert await writer.run_sync(current_revision) == "0030"
            await writer.exec_driver_sql("DROP INDEX ix_attempt_worker_open")

        report = await upgrade_to_run_event_metadata(empty_database_settings)
        repeated = await upgrade_to_run_event_metadata(empty_database_settings)
        async with database.read_transaction() as reader:
            after_attempt = (await reader.execute(select(*old_attempt))).all()
            after_dispatchers = (
                await reader.execute(
                    select(*old_dispatcher).order_by(attempt_dispatcher.c.id)
                )
            ).all()
            slots = list(
                await reader.scalars(select(attempt.c.current_dispatcher_open))
            )
            receipts = (
                await reader.execute(
                    select(
                        attempt_dispatcher.c.claim_id, attempt_dispatcher.c.claim_digest
                    )
                )
            ).all()
            differences = await reader.run_sync(historical_schema_differences)

        assert (report.from_revision, report.to_revision, report.steps) == (
            "0030",
            "run_event_metadata_0034",
            4,
        )
        assert repeated.steps == 0
        assert after_attempt == before_attempt
        assert after_dispatchers == before_dispatchers
        assert slots == [None]
        assert [tuple(row) for row in receipts] == [(None, None)] * 2
        assert differences == []
        assert database.units_in_flight() == ledger.outstanding == 0
    finally:
        await database.close()
    assert ledger.opened == ledger.closed
