from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Connection, insert, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.identifiers import encode
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.run_input_locator_v1 import (
    SQLITE_RUN_INPUT_KEY,
    SQLITE_RUN_INPUT_LOCATOR_INDEX,
    SQLITE_RUN_KEY,
    SQLITE_RUN_LOCATOR_INDEX,
)
from kora.db.tables import run, run_inputs
from tests.db.cancel_cause_support import migration_lock_count
from tests.db.seed import Seeder
from tests.db.test_graph_publication_digest_migration import assert_sqlite_index
from tests.db.test_migrations import differences_ignoring_expression_indexes
from tests.db.test_run_input_storage_migration import SENTINEL, UpgradeObservation
from tests.support.databases import encoded_sqlite_settings, sqlite_settings

PREDECESSOR = "0020"
HEAD_REVISION = "0023"
WIDE = 4 * 1024 * 1024
INDEXES = {SQLITE_RUN_LOCATOR_INDEX, SQLITE_RUN_INPUT_LOCATOR_INDEX}
PARENT_LOOKUP = "".join(
    (
        "SELECT id FROM main.run INDEXED BY ix_run_input_locator "
        "WHERE id COLLATE BINARY = ? AND (",
        SQLITE_RUN_KEY,
        ")",
    )
)
INPUT_LOOKUP = "".join(
    (
        "SELECT run_id FROM main.run_inputs INDEXED BY ix_run_inputs_locator "
        "WHERE run_id COLLATE BINARY = ? AND (",
        SQLITE_RUN_INPUT_KEY,
        ")",
    )
)
CONFLICT = "CREATE INDEX ix_run_inputs_locator ON run_inputs(run_id)"


def install_predecessor(connection: Connection) -> None:
    command.upgrade(alembic_config(connection), PREDECESSOR)


def downgrade_to_predecessor(connection: Connection) -> None:
    command.downgrade(alembic_config(connection), PREDECESSOR)


async def seed_runs(
    database: Database, historical_keys: tuple[str | bytes, ...] = ()
) -> tuple[str, str]:
    async with database.migration_connection() as connection:
        await connection.run_sync(install_predecessor)
    selected, without_input = encode(30), encode(31)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace = await seed.namespace()
        graph = await seed.graph(namespace)
        version = await seed.graph_version(graph)
        for key in (selected, without_input, *historical_keys):
            values = seed.run_values(namespace, graph, version)
            if key in (selected, without_input):
                values["id"] = key
            if key != without_input:
                values.update(
                    input_encoding_version=1,
                    params=SENTINEL,
                    cel_params_projection=SENTINEL,
                )
            await writer.execute(insert(run).values(**values))
            if key not in (selected, without_input):
                await writer.exec_driver_sql(
                    "UPDATE main.run SET id = ? WHERE id = ?", (key, values["id"])
                )
                await writer.exec_driver_sql(
                    "INSERT INTO main.run_inputs (run_id, cel_projection, params) "
                    "VALUES (?, ?, ?)",
                    (key, b"{}", b'{"keep":1}'),
                )
            elif key == selected:
                await writer.execute(
                    insert(run_inputs).values(
                        run_id=key, cel_projection=b"{}", params=b'{"keep":1}'
                    )
                )
    return selected, without_input


async def saved_rows(database: Database) -> tuple[tuple[tuple[object, ...], ...], ...]:
    async with database.read_transaction() as reader:
        with await reader.execute(select(run).order_by(run.c.id)) as parents:
            parent_rows = tuple(tuple(row) for row in parents)
        with await reader.execute(
            select(run_inputs).order_by(run_inputs.c.run_id)
        ) as inputs:
            input_rows = tuple(tuple(row) for row in inputs)
    return parent_rows, input_rows


async def native_indexes(reader: AsyncConnection) -> set[str]:
    if reader.dialect.name == "sqlite":
        with await reader.exec_driver_sql(
            "SELECT name FROM main.sqlite_schema WHERE type = 'index' "
            "AND tbl_name IN ('run', 'run_inputs')"
        ) as rows:
            return {str(row[0]) for row in rows}
    return await reader.run_sync(
        lambda connection: {
            str(index["name"])
            for table in ("run", "run_inputs")
            for index in inspect(connection).get_indexes(table)
        }
    )


async def point_lookup(
    reader: AsyncConnection, statement: str, key: str | bytes, index: str, column: str
) -> object:
    with await reader.exec_driver_sql(
        "EXPLAIN QUERY PLAN " + statement, (key,)
    ) as rows:
        plan = [tuple(row) for row in rows]
    with await reader.exec_driver_sql(statement, (key,)) as rows:
        result: object = rows.scalar_one_or_none()

    assert len(plan) == 1
    assert f"USING COVERING INDEX {index} ({column}=?)" in str(plan[0][3])
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
async def test_run_input_locators_exclude_noncanonical_historical_keys(
    tmp_path: Path, encoding: str
) -> None:
    settings = encoded_sqlite_settings(tmp_path / "locators.db", encoding)
    width_fitting_nul = (
        encode(20) + "\x00" + "X" * 25
        if encoding == "UTF-8"
        else encode(20)[:-1] + "\x00"
    )
    bad_keys = (
        "0" * WIDE,
        b"0" * WIDE,
        encode(25).encode(),
        width_fitting_nul,
        encode(30).lower(),
        "8" + encode(20)[1:],
    )
    database = Database(settings)
    try:
        selected, without_input = await seed_runs(database, bad_keys)
        missing = encode(32)
        keys = (selected, without_input, missing, *bad_keys)
        before = await saved_rows(database)

        report = await upgrade_database(settings)
        after = await saved_rows(database)
        async with database.read_transaction() as reader:
            observed_encoding: object = await reader.scalar(text("PRAGMA encoding"))
            with await reader.exec_driver_sql("PRAGMA foreign_key_check") as conflicts:
                foreign_key_errors = conflicts.all()
            parents = [
                await point_lookup(
                    reader, PARENT_LOOKUP, key, SQLITE_RUN_LOCATOR_INDEX, "id"
                )
                for key in keys
            ]
            inputs = [
                await point_lookup(
                    reader, INPUT_LOOKUP, key, SQLITE_RUN_INPUT_LOCATOR_INDEX, "run_id"
                )
                for key in keys
            ]
            await assert_sqlite_index(
                reader, SQLITE_RUN_LOCATOR_INDEX, ("id",), SQLITE_RUN_KEY
            )
            await assert_sqlite_index(
                reader,
                SQLITE_RUN_INPUT_LOCATOR_INDEX,
                ("run_id",),
                SQLITE_RUN_INPUT_KEY,
            )

        assert observed_encoding == encoding
        assert report.to_revision == HEAD_REVISION
        assert report.steps == 3
        assert before == after
        assert len(after[0]) == len(keys) - 1
        assert len(after[1]) == len(keys) - 2
        assert foreign_key_errors == []
        assert parents == [selected, without_input, *([None] * (len(keys) - 2))]
        assert inputs == [selected, *([None] * (len(keys) - 1))]
        assert database.units_in_flight() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_run_input_locator_migration_preserves_existing_schema_and_values(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    observation = UpgradeObservation()
    try:
        await seed_runs(database)
        before = await saved_rows(database)
        async with database.read_transaction() as reader:
            before_indexes = await native_indexes(reader)

        report = await upgrade_database(empty_database_settings, observation.open)
        repeat = await upgrade_database(empty_database_settings)
        with pytest.raises(RuntimeError, match="restore a compatible backup"):
            async with database.migration_connection() as connection:
                await connection.run_sync(downgrade_to_predecessor)
        after = await saved_rows(database)
        async with database.read_transaction() as reader:
            after_indexes = await native_indexes(reader)
            revision = await reader.run_sync(current_revision)
            differences = await reader.run_sync(differences_ignoring_expression_indexes)
        locks = await migration_lock_count(database)

        assert (report.from_revision, report.to_revision, report.steps) == (
            PREDECESSOR,
            HEAD_REVISION,
            3,
        )
        assert (repeat.from_revision, repeat.to_revision, repeat.steps) == (
            HEAD_REVISION,
            HEAD_REVISION,
            0,
        )
        assert revision == HEAD_REVISION
        assert differences == []
        assert before == after
        if database.dialect is Dialect.SQLITE:
            assert after_indexes == before_indexes | INDEXES | {"ix_run_status_id"}
        else:
            assert after_indexes == before_indexes | {"ix_run_status_id"}
            definitions = [
                statement
                for statement in observation.statements
                if statement.startswith(("CREATE ", "ALTER ", "DROP "))
                and (
                    statement.startswith(
                        (
                            "CREATE TABLE run ",
                            "ALTER TABLE run ",
                            "DROP TABLE run ",
                            "CREATE TABLE run_inputs ",
                            "ALTER TABLE run_inputs ",
                            "DROP TABLE run_inputs ",
                            "CREATE TABLE run_promotion_admission (",
                        )
                    )
                    or " ON run " in statement
                    or " ON run_inputs " in statement
                )
            ]
            assert len(definitions) == 2
            assert definitions[0] == (
                "CREATE INDEX CONCURRENTLY ix_run_status_id ON run (status, id)"
            )
            assert definitions[1].startswith("CREATE TABLE run_promotion_admission (")
        assert locks == database.units_in_flight() == 0
        observation.assert_released()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_run_input_locator_conflict_rolls_back_first_index_and_revision(
    tmp_path: Path,
) -> None:
    settings = sqlite_settings(tmp_path / "conflict.db")
    database = Database(settings)
    observation = UpgradeObservation()
    try:
        await seed_runs(database)
        before = await saved_rows(database)
        async with database.transaction() as writer:
            await writer.exec_driver_sql(CONFLICT)
            original_indexes = await native_indexes(writer)

        with pytest.raises(OperationalError, match="already exists"):
            await upgrade_database(settings, observation.open)
        async with database.transaction() as writer:
            refused_revision = await writer.run_sync(current_revision)
            refused_indexes = await native_indexes(writer)
            await writer.exec_driver_sql("DROP INDEX ix_run_inputs_locator")
        recovered = await upgrade_database(settings)
        after = await saved_rows(database)
        async with database.read_transaction() as reader:
            recovered_indexes = await native_indexes(reader)

        assert any(
            statement.startswith("CREATE INDEX ix_run_input_locator ON run ")
            for statement in observation.statements
        )
        assert refused_revision == PREDECESSOR
        assert refused_indexes == original_indexes
        assert SQLITE_RUN_LOCATOR_INDEX not in refused_indexes
        assert (recovered.from_revision, recovered.to_revision, recovered.steps) == (
            PREDECESSOR,
            HEAD_REVISION,
            3,
        )
        assert recovered_indexes == original_indexes | INDEXES | {"ix_run_status_id"}
        assert before == after
        assert database.units_in_flight() == 0
        observation.assert_released()
    finally:
        await database.close()
