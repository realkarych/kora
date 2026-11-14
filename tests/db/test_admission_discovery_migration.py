import asyncio

import pytest
from alembic import command
from sqlalchemy import select

from kora.db.admission_discovery_v1 import CANDIDATE_ROWS, INDEX_NAME
from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import node_instance
from kora.engine.admission_discovery import AdmissionSweep
from tests.db.cancel_cause_support import migration_lock_count
from tests.db.closing import ConnectionLedger
from tests.db.test_graph_publication_digest_migration import wait_for_invalid_index
from tests.engine.promotion_support import promotion_case


class InterruptedIndexBuild(RuntimeError):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_admission_discovery_migration_preserves_history_and_resumes(
    empty_database_settings: DatabaseSettings, interrupted: bool
) -> None:
    database = Database(empty_database_settings)
    ledger = ConnectionLedger(database)
    try:
        async with database.migration_connection() as migrator:
            await migrator.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), "0029")
            )
        case = await promotion_case(database)
        await case.promote(database)
        async with database.read_transaction() as reader:
            with await reader.execute(
                select(node_instance).order_by(node_instance.c.id)
            ) as rows:
                before = rows.all()
        create = (
            f"CREATE INDEX {INDEX_NAME} ON node_instance (id) WHERE {CANDIDATE_ROWS}"
        )
        if database.dialect is Dialect.POSTGRESQL:
            if interrupted:
                async with database.transaction() as writer:
                    await writer.exec_driver_sql(
                        "UPDATE node_instance SET status = status"
                    )
                    task = asyncio.create_task(
                        upgrade_database(empty_database_settings)
                    )
                    try:
                        await wait_for_invalid_index(database, INDEX_NAME)
                    finally:
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
            else:
                async with database.transaction() as writer:
                    await writer.exec_driver_sql(create)
        elif interrupted:

            async def rolled_back() -> None:
                async with database.transaction() as writer:
                    await writer.exec_driver_sql(create)
                    raise InterruptedIndexBuild()

            with pytest.raises(InterruptedIndexBuild):
                await rolled_back()

        upgraded = await upgrade_database(empty_database_settings)
        repeated = await upgrade_database(empty_database_settings)
        async with database.read_transaction() as reader:
            with await reader.execute(
                select(node_instance).order_by(node_instance.c.id)
            ) as rows:
                after = rows.all()
            revision = await reader.run_sync(current_revision)
            page = await AdmissionSweep().next_page(reader)

        assert (upgraded.from_revision, upgraded.to_revision, upgraded.steps) == (
            "0029",
            "0030",
            1,
        )
        assert repeated.steps == 0
        assert revision == "0030"
        assert before == after
        assert len(page.node_ids) == 2
        assert await migration_lock_count(database) == 0
        assert ledger.outstanding == database.units_in_flight() == 0
    finally:
        await database.close()
    assert ledger.opened == ledger.closed
