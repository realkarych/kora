import pytest
from alembic import command
from sqlalchemy import Connection, Table, inspect, select
from sqlalchemy.exc import DBAPIError, IntegrityError

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.migrations import (
    current_revision,
    upgrade_database,
    upgrade_with_diagnostics,
)
from kora.db.migrations.environment import alembic_config
from kora.db.tables import (
    graph,
    graph_deployment_preparation,
    graph_deployment_preparation_capacity,
    graph_deployment_stage,
    graph_deployment_stage_occurrence,
    graph_version,
    idempotency_record,
    storage_address_fence,
)
from kora.protocols.identity_records import MAX_COUNT
from tests.db.seed import DIGEST, START, Seeder
from tests.db.test_graph_deployment_stage_migration import (
    MAX_STAGE_ID,
    insert_fence,
    occurrence_values,
    prepare_graph,
    stage_values,
)
from tests.db.test_graph_read_admission import executed_queries

PREDECESSOR: str = "0011"
REVISION: str = "0012"
INSERT_PREFIX: str = "INSERT INTO graph_deployment_preparation_capacity "
RETAINED_RELATIONS: tuple[Table, ...] = (
    graph,
    graph_version,
    graph_deployment_preparation,
    storage_address_fence,
    graph_deployment_stage,
    graph_deployment_stage_occurrence,
    idempotency_record,
)


def capacity_columns(
    connection: Connection,
) -> tuple[list[str], list[tuple[str, str, bool, object, object]]]:
    inspector = inspect(connection)
    table_name = graph_deployment_preparation_capacity.name
    primary = inspector.get_pk_constraint(table_name)["constrained_columns"]
    columns = inspector.get_columns(table_name)
    return list(primary or ()), [
        (
            str(column["name"]),
            str(column["type"]),
            bool(column["nullable"]),
            column.get("default"),
            column.get("identity"),
        )
        for column in columns
    ]


async def insert_native_capacity(database: Database, value_sql: str) -> None:
    async with database.transaction() as connection:
        await connection.exec_driver_sql(INSERT_PREFIX + value_sql)


@pytest.mark.asyncio
async def test_capacity_native_singleton_is_empty_and_has_no_generated_key(
    database: Database,
) -> None:
    invalid = (
        "(occupied) VALUES (0)",
        "(id, occupied) VALUES (NULL, 0)",
        "(id, occupied) VALUES (2, 0)",
        "(id, occupied) VALUES (1, NULL)",
        "(id, occupied) VALUES (1, -1)",
        "(id, occupied) VALUES (1, 9223372036854775808)",
        "(id, occupied) VALUES (1, 'invalid')",
    )
    if database.dialect is Dialect.SQLITE:
        invalid += (
            "(id, occupied) VALUES (1.5, 0)",
            "(id, occupied) VALUES (1, 0.5)",
            "(id, occupied) VALUES (1, X'0102')",
        )

    async with database.read_transaction() as connection:
        schema = await connection.run_sync(capacity_columns)
        empty = (
            await connection.execute(select(graph_deployment_preparation_capacity))
        ).all()
    for value_sql in invalid:
        with pytest.raises(DBAPIError):
            await insert_native_capacity(database, value_sql)
    await insert_native_capacity(database, "(id, occupied) VALUES (1, 0)")
    with pytest.raises(IntegrityError):
        await insert_native_capacity(database, "(id, occupied) VALUES (1, 0)")
    async with database.transaction() as connection:
        table = graph_deployment_preparation_capacity
        zero = (await connection.execute(select(table))).one()
        await connection.execute(table.update().values(occupied=MAX_COUNT))
        maximum = (await connection.execute(select(table))).one()
        if database.dialect is Dialect.SQLITE:
            classes = (
                await connection.exec_driver_sql(
                    "SELECT typeof(id), typeof(occupied) "
                    "FROM graph_deployment_preparation_capacity"
                )
            ).one()
        else:
            classes = None

    assert schema == (
        ["id"],
        [
            ("id", "SMALLINT", False, None, None),
            ("occupied", "BIGINT", False, None, None),
        ],
    )
    assert empty == []
    assert zero == (1, 0)
    assert maximum == (1, MAX_COUNT)
    if database.dialect is Dialect.SQLITE:
        assert classes == ("integer", "integer")


async def retained_history(
    database: Database,
) -> tuple[tuple[dict[str, object], ...], ...]:
    rows: list[tuple[dict[str, object], ...]] = []
    async with database.read_transaction() as connection:
        for relation in RETAINED_RELATIONS:
            result = await connection.execute(select(relation))
            rows.append(tuple(dict(row) for row in result.mappings()))
    return tuple(rows)


async def populate_predecessor(database: Database) -> None:
    async with database.migration_connection() as connection:
        await connection.run_sync(
            lambda sync: command.upgrade(alembic_config(sync), PREDECESSOR)
        )
    async with database.transaction() as connection:
        scope = await prepare_graph(connection)
        incarnation = await insert_fence(connection, "Store")
        await connection.execute(
            graph_deployment_stage.insert().values(
                **stage_values(scope, MAX_STAGE_ID, incarnation)
            )
        )
        await connection.execute(
            graph_deployment_stage_occurrence.insert().values(
                **occurrence_values(scope, MAX_STAGE_ID, 0)
            )
        )
        seeder = Seeder(connection)
        token_id = await seeder.token(DIGEST)
        version_id = (await connection.execute(select(graph_version.c.id))).scalar_one()
        await connection.execute(
            idempotency_record.insert().values(
                token_id=token_id,
                key="graph-deploy",
                request_digest=DIGEST,
                response={"graph_version_id": version_id},
                expires_at=START,
                **seeder.stamps(),
            )
        )


@pytest.mark.asyncio
async def test_capacity_migration_preserves_populated_history_and_creates_no_witness(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        await populate_predecessor(database)
        history = await retained_history(database)

        async with database.migration_connection() as connection:
            with executed_queries(connection) as observed:
                report = await connection.run_sync(upgrade_with_diagnostics)
        repeated = await upgrade_database(empty_database_settings)
        with pytest.raises(RuntimeError, match="capacity cannot be downgraded"):
            async with database.migration_connection() as connection:
                await connection.run_sync(
                    lambda sync: command.downgrade(alembic_config(sync), PREDECESSOR)
                )
        surviving = await retained_history(database)
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            capacity = (
                await connection.execute(select(graph_deployment_preparation_capacity))
            ).all()
    finally:
        await database.close()

    assert (report.from_revision, report.to_revision, report.steps) == (
        PREDECESSOR,
        REVISION,
        1,
    )
    assert (repeated.from_revision, repeated.to_revision, repeated.steps) == (
        REVISION,
        REVISION,
        0,
    )
    assert revision == REVISION
    assert all(len(rows) == 1 for rows in history)
    assert surviving == history
    assert capacity == []
    statements = [statement.lower() for statement, _ in observed]
    assert (
        sum(
            statement.lstrip().startswith(
                "create table graph_deployment_preparation_capacity"
            )
            for statement in statements
        )
        == 1
    )
    for statement in statements:
        for relation in RETAINED_RELATIONS:
            assert not any(
                f"{prefix}{relation.name}" in statement
                for prefix in (
                    "from ",
                    "join ",
                    "insert into ",
                    "update ",
                    "delete from ",
                )
            )
