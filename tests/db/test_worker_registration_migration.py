import pytest
from alembic import command
from sqlalchemy import Connection, insert, select
from sqlalchemy.exc import IntegrityError

from kora.db.connection import Database, DatabaseSettings
from kora.db.migrations import upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import event, worker
from tests.db.seed import START, Seeder
from tests.plugins.authentication.support import seed_case


def predecessor(connection: Connection) -> None:
    command.upgrade(alembic_config(connection), "0021")


@pytest.mark.asyncio
async def test_worker_registration_migration_preserves_existing_workers_and_events(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        async with database.migration_connection() as connection:
            await connection.run_sync(predecessor)
        case = await seed_case(database, "worker", "service")
        async with database.transaction() as connection:
            seed = Seeder(connection)
            await seed.worker(case.principal.token_id)
            await connection.execute(
                insert(event).values(
                    id=seed.new_id(),
                    namespace_id=case.principal.namespaces[0].namespace_id,
                    kind="fixture.original",
                    actor={"kind": "system"},
                    payload={"preserved": True},
                    at=START,
                    **seed.stamps(),
                )
            )
            before_worker = (await connection.execute(select(worker))).mappings().one()
            before_event = (await connection.execute(select(event))).mappings().one()

        await database.close()
        result = await upgrade_database(empty_database_settings)
        database = Database(empty_database_settings)
        async with database.read_transaction() as connection:
            after_worker = (await connection.execute(select(worker))).mappings().one()
            after_event = (await connection.execute(select(event))).mappings().one()

        assert result.from_revision == "0021"
        assert result.to_revision == "0022"
        assert after_worker == before_worker
        assert after_event == before_event
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "run_id"), [("run.created", None), ("worker.registered", "1" * 26)]
)
async def test_only_unscoped_worker_registration_can_omit_namespace(
    database: Database,
    kind: str,
    run_id: str | None,
) -> None:
    async with database.transaction() as connection:
        seed = Seeder(connection)
        identity = seed.new_id()

    with pytest.raises(IntegrityError) as refused:
        async with database.transaction() as connection:
            await connection.execute(
                insert(event).values(
                    id=identity,
                    namespace_id=None,
                    run_id=run_id,
                    kind=kind,
                    actor={"kind": "service"},
                    payload={},
                    at=START,
                    created_at=START,
                    updated_at=START,
                )
            )
    async with database.read_transaction() as connection:
        rows = (await connection.execute(select(event))).all()

    assert rows == []
    assert "ck_event_namespace_scope" in str(refused.value)
