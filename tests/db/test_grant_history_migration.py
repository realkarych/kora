import pytest
from alembic import command
from sqlalchemy import Connection, insert, inspect, select, update
from sqlalchemy.exc import SQLAlchemyError

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.grant_history_v1 import ACK_FIELDS, TIP_FIELDS
from kora.db.migrations import upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import attempt, attempt_dispatcher
from tests.db.seed import START, Seeder
from tests.db.test_migrations import differences_ignoring_expression_indexes
from tests.engine.test_grant_history import legacy, seed


def predecessor(connection: Connection) -> None:
    command.upgrade(alembic_config(connection), "0028")


def downgrade(connection: Connection) -> None:
    command.downgrade(alembic_config(connection), "0028")


@pytest.mark.asyncio
async def test_grant_history_migration_preserves_active_legacy_rows(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    old_attempt = tuple(
        field for field in attempt.c if field.name not in (*TIP_FIELDS, *ACK_FIELDS)
    )
    old_dispatcher = tuple(
        field
        for field in attempt_dispatcher.c
        if field.name not in ("process", "prefix_count", "prefix_digest")
    )
    try:
        async with database.migration_connection() as connection:
            await connection.run_sync(predecessor)
        async with database.transaction() as writer:
            seed = Seeder(writer)
            namespace = await seed.namespace()
            graph = await seed.graph(namespace)
            version = await seed.graph_version(graph)
            run = await seed.run(namespace, graph, version)
            node = await seed.node(run, "task")
            physical = await seed.attempt(node)
            token = await seed.token("b" * 64)
            worker = await seed.worker(token)
            domain, incarnation = seed.new_id(), seed.new_id()
            values: dict[str, object] = {
                "id": seed.new_id(),
                "attempt_id": physical,
                "worker_id": worker,
                "execution_domain_id": domain,
                "incarnation_id": incarnation,
                "lease_epoch": 3,
                "mode": "execute",
                "granted_at": START,
                "evidence": {"retained": True},
                "created_at": START,
                "updated_at": START,
            }
            await writer.execute(insert(attempt_dispatcher).values(**values))
            await writer.execute(
                update(attempt)
                .where(attempt.c.id == physical)
                .values(
                    execution_domain_id=domain,
                    worker_id=worker,
                    lease_epoch=3,
                    lease_until=START,
                )
            )
            before_attempt = (await writer.execute(select(*old_attempt))).one()
            before_dispatcher = (await writer.execute(select(*old_dispatcher))).one()

        await database.close()
        report = await upgrade_database(empty_database_settings)
        database = Database(empty_database_settings)
        async with database.read_transaction() as reader:
            after_attempt = (await reader.execute(select(*old_attempt))).one()
            after_dispatcher = (await reader.execute(select(*old_dispatcher))).one()
            unknown = (
                await reader.execute(
                    select(
                        attempt_dispatcher.c.process,
                        attempt_dispatcher.c.prefix_count,
                        attempt_dispatcher.c.prefix_digest,
                    )
                )
            ).one()
            tip = (
                await reader.execute(
                    select(*(attempt.c[name] for name in (*TIP_FIELDS, *ACK_FIELDS)))
                )
            ).one()
            differences = await reader.run_sync(differences_ignoring_expression_indexes)
        async with database.transaction() as writer:
            values.update(
                id=Seeder(writer).new_id(),
                lease_epoch=2**40,
                execution_domain_id="d" * 256,
                incarnation_id="i" * 256,
            )
            await writer.execute(insert(attempt_dispatcher).values(**values))
        async with database.migration_connection() as connection:
            with pytest.raises(
                RuntimeError, match="grant history cannot be downgraded"
            ):
                await connection.run_sync(downgrade)

        assert (report.from_revision, report.to_revision, report.steps) == (
            "0028",
            "0029",
            1,
        )
        assert after_attempt == before_attempt
        assert after_dispatcher == before_dispatcher
        assert tuple(unknown) == (None, None, None)
        assert tuple(tip) == (None,) * 6
        assert differences == []
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix_state", ["valid", "invalid"])
async def test_grant_history_upgrade_resumes_after_second_index_failure(
    empty_database_settings: DatabaseSettings,
    prefix_state: str,
) -> None:
    database = Database(empty_database_settings)
    try:
        async with database.migration_connection() as connection:
            await connection.run_sync(predecessor)
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "CREATE TABLE ix_dispatcher_open_epoch (id INTEGER)"
            )
        await database.close()

        with pytest.raises(SQLAlchemyError):
            await upgrade_database(empty_database_settings)
        database = Database(empty_database_settings)
        async with database.read_transaction() as reader:
            columns = await reader.run_sync(
                lambda connection: inspect(connection).get_columns("attempt_dispatcher")
            )
        async with database.transaction() as writer:
            await writer.exec_driver_sql("DROP TABLE ix_dispatcher_open_epoch")
        if database.dialect is Dialect.POSTGRESQL and prefix_state == "invalid":
            scope = await seed(database)
            await legacy(database, scope, range(1, 3))
            async with database.transaction() as writer:
                await writer.exec_driver_sql("DROP INDEX ix_dispatcher_verified_prefix")
            async with database.migration_connection() as migrator:
                await migrator.rollback()
                await migrator.execution_options(isolation_level="AUTOCOMMIT")
                with pytest.raises(SQLAlchemyError):
                    await migrator.exec_driver_sql(
                        "CREATE UNIQUE INDEX CONCURRENTLY "
                        "ix_dispatcher_verified_prefix ON attempt_dispatcher "
                        "(attempt_id)"
                    )
            async with database.read_transaction() as reader:
                assert (
                    await reader.exec_driver_sql(
                        "SELECT indisvalid FROM pg_index WHERE indexrelid = "
                        "'ix_dispatcher_verified_prefix'::regclass"
                    )
                ).scalar_one() is False
        await database.close()
        recovered = await upgrade_database(empty_database_settings)
        database = Database(empty_database_settings)
        async with database.read_transaction() as reader:
            differences = await reader.run_sync(differences_ignoring_expression_indexes)

        assert ("prefix_count" in {column["name"] for column in columns}) == (
            database.dialect is Dialect.POSTGRESQL
        )
        assert (recovered.from_revision, recovered.to_revision, recovered.steps) == (
            "0028",
            "0029",
            1,
        )
        assert differences == []
    finally:
        await database.close()
