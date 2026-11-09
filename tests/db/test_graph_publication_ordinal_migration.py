import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.graph_publication_ordinal_v1 import (
    ORDINAL_INVALID_COLUMNS,
    ORDINAL_INVALID_INDEX,
    ORDINAL_VALID_COLUMNS,
    ORDINAL_VALID_INDEX,
    SQLITE_ORDINAL_INVALID,
    SQLITE_ORDINAL_VALID,
)
from kora.db.migrations import (
    MigrationReport,
    current_revision,
    upgrade_database,
    upgrade_to_head,
)
from kora.db.migrations.environment import (
    alembic_config,
    bound_lock_waits,
    pending_steps,
)
from kora.db.tables import (
    graph_deployment_stage_promotion_intent,
    graph_deployment_stage_promotion_settlement,
    graph_version,
)
from kora.engine.locks import SQLITE_ORDINAL_OBSERVATION
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_deployment_stage_migration import insert_fence
from tests.db.test_graph_publication_digest_migration import (
    assert_sqlite_index,
    history,
)
from tests.db.test_graph_read_admission import executed_queries, sqlite_instructions
from tests.db.test_graph_stage_promotion_custody_migration import (
    INTENT_INSERT,
    SETTLEMENT_INSERT,
    seed_stage,
)
from tests.engine.test_graph_publication_ordinal import read_ordinal
from tests.engine.test_graph_publication_target import seed_target
from tests.support.databases import sqlite_settings

PREDECESSOR = "0016"
REVISION = "0017"
SMALL_BLOB = 64 * 1024
WIDE_BLOB = 4 * 1024 * 1024
INDEXES = {ORDINAL_VALID_INDEX, ORDINAL_INVALID_INDEX}
BASE_READ_OPS = {
    "Column",
    "FinishSeek",
    "Last",
    "Next",
    "NotExists",
    "Prev",
    "Rewind",
    "RowData",
    "Rowid",
    "SeekGE",
    "SeekGT",
    "SeekLE",
    "SeekLT",
    "SeekRowid",
}


def upgrade_predecessor(connection: Connection) -> None:
    command.upgrade(alembic_config(connection), PREDECESSOR)


def upgrade_ordinal_revision(connection: Connection) -> MigrationReport:
    bound_lock_waits(connection)
    config = alembic_config(connection)
    predecessor = current_revision(connection)
    steps = pending_steps(config, predecessor, REVISION)
    if connection.dialect.name == str(Dialect.POSTGRESQL):
        connection.commit()
    command.upgrade(config, REVISION)
    return MigrationReport(connection.dialect.name, predecessor, REVISION, steps)


async def upgrade_ordinal(settings: DatabaseSettings) -> MigrationReport:
    owner = Database(settings)
    try:
        async with owner.migration_connection() as connection:
            return await connection.run_sync(upgrade_ordinal_revision)
    finally:
        await owner.close()


def downgrade_to_predecessor(connection: Connection) -> None:
    command.downgrade(alembic_config(connection), PREDECESSOR)


def reflected_indexes(connection: Connection) -> set[str]:
    names = (
        entry["name"] for entry in inspect(connection).get_indexes("graph_version")
    )
    return {name for name in names if name is not None}


async def seed_custody(connection: AsyncConnection) -> None:
    incarnation = await insert_fence(connection, "Store")
    original = await seed_stage(connection, "ordinal-retained", incarnation=incarnation)
    await connection.execute(
        INTENT_INSERT, original.request("ordinal-retained-op", b'{"intent":1}')
    )
    await connection.execute(
        SETTLEMENT_INSERT,
        {"stage_id": original.stage_id, "observation_bytes": b'{"settlement":1}'},
    )


async def custody_rows(connection: AsyncConnection) -> tuple[object, ...]:
    intent = (
        await connection.execute(select(graph_deployment_stage_promotion_intent))
    ).one()
    settlement = (
        await connection.execute(select(graph_deployment_stage_promotion_settlement))
    ).one()
    return tuple(intent), tuple(settlement)


async def replace_with_blob(writer: AsyncConnection, version: str, width: int) -> None:
    await writer.execute(
        text("UPDATE graph_version SET number = zeroblob(:width) WHERE id = :version"),
        {"width": width, "version": version},
    )


async def native_observations(
    reader: AsyncConnection, selected: tuple[str, ...]
) -> tuple[list[object], list[tuple[str, tuple[object, ...]]]]:
    results: list[object] = []
    with executed_queries(reader) as statements:
        for graph_id in selected:
            result = await reader.execute(
                SQLITE_ORDINAL_OBSERVATION, {"graph_id": graph_id}
            )
            try:
                results.append(result.scalar_one())
            finally:
                result.close()
    return results, statements


async def assert_no_sqlite_base_value_read(
    reader: AsyncConnection, statement: str, parameters: tuple[object, ...]
) -> None:
    rows = (
        await reader.execute(
            text(
                "SELECT name, rootpage FROM main.sqlite_schema "
                "WHERE name IN ('graph_version', :valid, :invalid)"
            ),
            {"valid": ORDINAL_VALID_INDEX, "invalid": ORDINAL_INVALID_INDEX},
        )
    ).all()
    roots: dict[str, int] = {}
    for name, root_page in rows:
        assert type(name) is str
        assert type(root_page) is int
        roots[name] = root_page
    instructions = await sqlite_instructions(reader, statement, parameters)
    table = {
        row[2]
        for row in instructions
        if row[1] == "OpenRead" and row[3] == roots["graph_version"]
    }
    valid = {
        row[2]
        for row in instructions
        if row[1] == "OpenRead" and row[3] == roots[ORDINAL_VALID_INDEX]
    }
    invalid = {
        row[2]
        for row in instructions
        if row[1] == "OpenRead" and row[3] == roots[ORDINAL_INVALID_INDEX]
    }
    assert valid
    assert invalid
    assert not any(row[2] in table and row[1] in BASE_READ_OPS for row in instructions)
    assert not any(row[1] == "IdxRowid" for row in instructions)
    assert [(row[2], row[3]) for row in instructions if row[1] == "Column"] == [
        (next(iter(valid)), 1)
    ]


@pytest.mark.asyncio
async def test_graph_ordinal_migration_preserves_rows_and_unique_identity(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        async with database.migration_connection() as connection:
            await connection.run_sync(upgrade_predecessor)
        binding, selected = await seed_target(database)
        async with database.transaction() as writer:
            await Seeder(writer).graph_version(selected, 7, ir={"legacy": "value"})
            await seed_custody(writer)
            original_custody = await custody_rows(writer)
        before = await history(database)

        report = await upgrade_ordinal(empty_database_settings)
        again = await upgrade_ordinal(empty_database_settings)
        with pytest.raises(
            RuntimeError, match="ordinal migration cannot be downgraded"
        ):
            async with database.migration_connection() as connection:
                await connection.run_sync(downgrade_to_predecessor)
        after = await history(database)
        observed = await read_ordinal(database, binding, selected)
        async with database.read_transaction() as reader:
            native_indexes = await reader.run_sync(reflected_indexes)
            revision = await reader.run_sync(current_revision)
            retained_custody = await custody_rows(reader)
        with pytest.raises(IntegrityError):
            async with database.transaction() as writer:
                await Seeder(writer).graph_version(selected, 7)

        assert (report.from_revision, report.to_revision, report.steps) == (
            PREDECESSOR,
            REVISION,
            1,
        )
        assert (again.from_revision, again.to_revision, again.steps) == (
            REVISION,
            REVISION,
            0,
        )
        assert revision == REVISION
        assert before == after
        assert retained_custody == original_custody
        assert observed == 7
        assert native_indexes & INDEXES == (
            INDEXES if database.dialect is Dialect.SQLITE else set()
        )
        assert database.writer_is_free()
        assert database.units_in_flight() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
async def test_migrated_sqlite_ordinal_indexes_exclude_wide_number_payloads(
    tmp_path: Path, encoding: str
) -> None:
    settings = sqlite_settings(tmp_path / "ordinal.db")
    with closing(sqlite3.connect(settings.sqlite_path)) as native:
        native.execute(f"PRAGMA encoding = '{encoding}'")
        native.execute("CREATE TABLE encoding_seed (value INTEGER)")
        native.execute("DROP TABLE encoding_seed")
        native.commit()
    database = Database(settings)
    try:
        async with database.migration_connection() as connection:
            await connection.run_sync(upgrade_predecessor)
        binding, selected = await seed_target(database)
        async with database.transaction() as writer:
            seed = Seeder(writer)
            small = await seed.graph(binding.namespace_id, "small")
            large = await seed.graph(binding.namespace_id, "large")
            low = await seed.graph(binding.namespace_id, "low")
            empty = await seed.graph(binding.namespace_id, "empty")
            await seed.graph_version(selected, 3, ir={"wide": "x" * SMALL_BLOB})
            await seed.graph_version(small, 1)
            await seed.graph_version(large, 1)
            await seed.graph_version(low, 1)
            await seed.graph_version(low, -1)
            small_version = await seed.graph_version(small, 2)
            large_version = await seed.graph_version(large, 2)
            await replace_with_blob(writer, small_version, SMALL_BLOB)
            await replace_with_blob(writer, large_version, WIDE_BLOB)

        await upgrade_database(settings)
        async with database.read_transaction() as reader:
            initial, statements = await native_observations(
                reader, (selected, small, large, low, empty)
            )
            await assert_sqlite_index(
                reader, ORDINAL_VALID_INDEX, ORDINAL_VALID_COLUMNS, SQLITE_ORDINAL_VALID
            )
            await assert_sqlite_index(
                reader,
                ORDINAL_INVALID_INDEX,
                ORDINAL_INVALID_COLUMNS,
                SQLITE_ORDINAL_INVALID,
            )
            for statement, parameters in statements:
                await assert_no_sqlite_base_value_read(reader, statement, parameters)
            matched_blob: object = await reader.scalar(
                text(
                    "SELECT number = zeroblob(:width) FROM graph_version "
                    "WHERE id = :version"
                ),
                {"width": WIDE_BLOB, "version": large_version},
            )
        async with database.transaction() as writer:
            await writer.execute(
                graph_version.update()
                .where(graph_version.c.id == small_version)
                .values(number=5)
            )
            new_version = await Seeder(writer).graph_version(selected, 9)
            promoted, _ = await native_observations(writer, (selected, small))
            await replace_with_blob(writer, new_version, WIDE_BLOB)
            moved, _ = await native_observations(writer, (selected, small))
            await writer.execute(
                graph_version.update()
                .where(graph_version.c.id == new_version)
                .values(number=9)
            )
            restored, _ = await native_observations(writer, (selected, small))

        assert initial == [3, "refused", "refused", "refused", None]
        assert matched_blob == 1
        assert promoted == [9, 5]
        assert moved == ["refused", 5]
        assert restored == promoted
        assert database.units_in_flight() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_sqlite_ordinal_migration_rolls_back_first_index_on_second_conflict(
    tmp_path: Path,
) -> None:
    settings = sqlite_settings(tmp_path / "ordinal-conflict.db")
    database = Database(settings)
    ledger = ConnectionLedger(database)
    try:
        async with database.migration_connection() as connection:
            await connection.run_sync(upgrade_predecessor)
        binding, selected = await seed_target(database)
        async with database.transaction() as writer:
            await Seeder(writer).graph_version(selected, 6)
            await seed_custody(writer)
            original_custody = await custody_rows(writer)
            await writer.exec_driver_sql(
                f"CREATE INDEX {ORDINAL_INVALID_INDEX} ON graph_version (number)"
            )
        before = await history(database)

        statements: list[tuple[str, tuple[object, ...]]] = []

        async def upgrade_conflicting_indexes() -> None:
            async with database.migration_connection() as connection:
                with executed_queries(connection) as captured:
                    try:
                        await connection.run_sync(upgrade_to_head)
                    finally:
                        statements.extend(captured)

        with pytest.raises(OperationalError, match="already exists"):
            await upgrade_conflicting_indexes()
        released = ledger.outstanding == database.units_in_flight() == 0
        async with database.transaction() as writer:
            revision = await writer.run_sync(current_revision)
            retained_indexes = await writer.run_sync(reflected_indexes)
            failed_custody = await custody_rows(writer)
            await writer.exec_driver_sql(f"DROP INDEX {ORDINAL_INVALID_INDEX}")
        recovered = await upgrade_database(settings)
        after = await history(database)
        observed = await read_ordinal(database, binding, selected)
        async with database.read_transaction() as reader:
            recovered_custody = await custody_rows(reader)

        assert [
            index
            for statement, _ in statements
            for index in (ORDINAL_VALID_INDEX, ORDINAL_INVALID_INDEX)
            if statement.startswith(f"CREATE INDEX {index} ")
        ] == [ORDINAL_VALID_INDEX, ORDINAL_INVALID_INDEX]
        assert released
        assert revision == PREDECESSOR
        assert retained_indexes & INDEXES == {ORDINAL_INVALID_INDEX}
        assert (recovered.from_revision, recovered.to_revision, recovered.steps) == (
            PREDECESSOR,
            "0020",
            4,
        )
        assert before == after
        assert failed_custody == recovered_custody == original_custody
        assert observed == 6
        assert database.writer_is_free()
        assert ledger.outstanding == database.units_in_flight() == 0
    finally:
        await database.close()
