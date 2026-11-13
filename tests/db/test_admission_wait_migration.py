import asyncio

import pytest
from alembic import command
from sqlalchemy import insert, inspect, select
from sqlalchemy.exc import DBAPIError

from kora.db.admission_wait_v1 import INDEXES, wait_index_dialect
from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import wait
from tests.db.cancel_cause_support import migration_lock_count
from tests.db.closing import ConnectionLedger
from tests.db.seed import START, Seeder
from tests.db.test_graph_publication_digest_migration import wait_for_invalid_index
from tests.engine.admission_wait_support import admission_case


@pytest.mark.asyncio
@pytest.mark.parametrize("retained_index", ["invalid", "valid"])
async def test_admission_wait_migration_preserves_history_and_recovers_indexes(
    empty_database_settings: DatabaseSettings, retained_index: str
) -> None:
    database = Database(empty_database_settings)
    ledger = ConnectionLedger(database)
    owned_index = (
        "ix_wait_admission_due"
        if database.dialect is Dialect.POSTGRESQL
        else "ix_wait_open_owner"
    )
    try:
        async with database.migration_connection() as migrator:
            await migrator.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), "0027")
            )
        case = await admission_case(database)
        async with database.transaction() as writer:
            seed = Seeder(writer)
            for status in ("open", "cancelled"):
                await writer.execute(
                    insert(wait).values(
                        id=seed.new_id(),
                        node_instance_id=case.binding.node_id,
                        kind="admission",
                        status=status,
                        wake_at=START,
                        reason={"private": "legacy"},
                        observation={"current": None},
                        created_at=START,
                        updated_at=START,
                    )
                )
        async with database.read_transaction() as reader:
            with await reader.execute(select(wait)) as result:
                before = result.all()
        async with database.transaction() as writer:
            await writer.exec_driver_sql(f"CREATE INDEX {owned_index} ON wait (id)")
        with pytest.raises(
            (RuntimeError, DBAPIError), match="conflicts|already exists"
        ):
            await upgrade_database(empty_database_settings)
        async with database.transaction() as writer:
            assert await writer.run_sync(current_revision) == "0027"
            await writer.exec_driver_sql(f"DROP INDEX {owned_index}")
        if database.dialect is Dialect.POSTGRESQL:
            if retained_index == "invalid":
                async with database.transaction() as writer:
                    await writer.exec_driver_sql("UPDATE wait SET status = status")
                    task = asyncio.create_task(
                        upgrade_database(empty_database_settings)
                    )
                    try:
                        await wait_for_invalid_index(database, owned_index)
                    finally:
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
            else:
                async with database.transaction() as writer:
                    for name, table, columns, _, predicate, _ in INDEXES:
                        if wait_index_dialect(name) is not None:
                            continue
                        suffix = " WHERE " + predicate
                        await writer.exec_driver_sql(
                            f"CREATE INDEX {name} ON {table} "
                            f"({', '.join(columns)}){suffix}"
                        )

        report = await upgrade_database(empty_database_settings)
        repeated = await upgrade_database(empty_database_settings)
        async with database.read_transaction() as reader:
            with await reader.execute(select(wait)) as result:
                after = result.all()
            indexes = await reader.run_sync(
                lambda sync: {
                    entry["name"] for entry in inspect(sync).get_indexes("wait")
                }
            )

        assert (report.from_revision, report.to_revision, report.steps) == (
            "0027",
            "0028",
            1,
        )
        assert repeated.steps == 0
        assert after == before
        assert "ix_wait_node_status" in indexes
        assert "ix_wait_admission_due" in indexes
        assert ("ix_wait_open_owner" in indexes) is (database.dialect is Dialect.SQLITE)
        assert await migration_lock_count(database) == 0
        assert database.units_in_flight() == ledger.outstanding == 0
    finally:
        await database.close()
    assert ledger.opened == ledger.closed
