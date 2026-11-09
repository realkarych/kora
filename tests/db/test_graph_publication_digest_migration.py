import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Connection, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.graph_preparation_reads import GraphPreparationBinding
from kora.db.graph_publication_digest_v1 import (
    DIGEST_INDEX,
    INVALID_ID_INDEX,
    LOCATOR_COLUMNS,
    SQLITE_INVALID,
    SQLITE_VALID,
    VALID_COLUMNS,
)
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import (
    MigrationReport,
    alembic_config,
    pending_steps,
)
from kora.db.tables import graph_version
from kora.engine.locks import SQLITE_DIGEST_MATCH
from tests.db.cancel_cause_support import migration_lock_count
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, Seeder
from tests.db.test_graph_read_admission import executed_queries, sqlite_instructions
from tests.engine.lock_support import DEADLINE
from tests.engine.test_graph_publication_digest import OTHER_DIGEST, read_digest
from tests.engine.test_graph_publication_target import seed_target
from tests.support.databases import sqlite_settings

PREDECESSOR = "0013"
REVISION = "0014"
WIDE = 2 * 1024 * 1024
BLOB_ID_DIGEST = "3" * 64
NUL_ID_DIGEST = "4" * 64


def upgrade_predecessor(connection: Connection) -> None:
    command.upgrade(alembic_config(connection), PREDECESSOR)


def downgrade_locator(connection: Connection) -> None:
    command.downgrade(alembic_config(connection), PREDECESSOR)


def upgrade_digest_revision(connection: Connection) -> MigrationReport:
    config = alembic_config(connection)
    predecessor = current_revision(connection)
    steps = pending_steps(config, predecessor, REVISION)
    if connection.dialect.name == "postgresql":
        connection.commit()
    command.upgrade(config, REVISION)
    return MigrationReport(connection.dialect.name, predecessor, REVISION, steps)


async def upgrade_digest(
    settings: DatabaseSettings,
    open_database: Callable[[DatabaseSettings], Database] = Database,
) -> MigrationReport:
    database = open_database(settings)
    try:
        async with database.migration_connection() as connection:
            return await connection.run_sync(upgrade_digest_revision)
    finally:
        await database.close()


async def seed_predecessor(
    database: Database, *, wide: bool = False
) -> tuple[GraphPreparationBinding, str, str]:
    async with database.migration_connection() as connection:
        await connection.run_sync(upgrade_predecessor)
    binding, selected = await seed_target(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        version = await seeder.graph_version(
            selected, ir={"legacy": "x" * WIDE} if wide else {"legacy": "small"}
        )
    return binding, selected, version


async def history(database: Database) -> tuple[tuple[object, ...], ...]:
    async with database.read_transaction() as connection:
        result = await connection.execute(
            select(graph_version).order_by(graph_version.c.number)
        )
        return tuple(tuple(row) for row in result)


@pytest.mark.asyncio
async def test_graph_digest_locator_migration_preserves_history(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        binding, selected, version = await seed_predecessor(database)
        before = await history(database)

        report = await upgrade_digest(empty_database_settings)
        repeated = await upgrade_digest(empty_database_settings)
        with pytest.raises(RuntimeError, match="digest migration cannot be downgraded"):
            async with database.migration_connection() as connection:
                await connection.run_sync(downgrade_locator)
        after = await history(database)
        found = await read_digest(database, binding, selected)
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)

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
        assert after == before
        assert found == version
        assert await migration_lock_count(database) == 0
        assert database.units_in_flight() == 0
    finally:
        await database.close()


async def assert_sqlite_index(
    connection: AsyncConnection, name: str, keys: tuple[str, ...], predicate: str
) -> None:
    sql: object = await connection.scalar(
        text("SELECT sql FROM sqlite_schema WHERE type = 'index' AND name = :name"),
        {"name": name},
    )
    rows = (
        await connection.execute(
            text("SELECT name, coll, desc, key FROM pragma_index_xinfo(:name)"),
            {"name": name},
        )
    ).all()

    assert type(sql) is str
    assert sql.split(" WHERE ", 1)[1] == predicate
    assert [tuple(row) for row in rows if row[3] == 1] == [
        (key, "BINARY", 0, 1) for key in keys
    ]


async def insert_legacy_anomalies(
    writer: AsyncConnection, selected: str, width_fitting_nul: str
) -> None:
    seeder = Seeder(writer)
    anomalous: tuple[tuple[str | bytes, str], ...] = (
        ("9" * WIDE, DIGEST),
        (b"B" * 26, BLOB_ID_DIGEST),
        (width_fitting_nul, NUL_ID_DIGEST),
    )
    for number, (value, digest) in enumerate(anomalous, start=2):
        original = await seeder.graph_version(selected, number)
        await writer.execute(
            text(
                "UPDATE graph_version SET id = :bad, deployment_digest = :digest "
                "WHERE id = :original"
            ),
            {"bad": value, "digest": digest, "original": original},
        )
    lookalike = await seeder.graph_version(selected, 5)
    await writer.execute(
        text("UPDATE graph_version SET deployment_digest = :wide WHERE id = :version"),
        {"wide": OTHER_DIGEST + "z" * WIDE, "version": lookalike},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
async def test_migrated_sqlite_digest_lookup_never_loads_base_values(
    tmp_path: Path, encoding: str
) -> None:
    settings = sqlite_settings(tmp_path / "digest.db")
    with closing(sqlite3.connect(settings.sqlite_path)) as native:
        native.execute(f"PRAGMA encoding = '{encoding}'")
        native.execute("CREATE TABLE encoding_seed (value INTEGER)")
        native.execute("DROP TABLE encoding_seed")
        native.commit()
    database = Database(settings)
    try:
        _, selected, version = await seed_predecessor(database, wide=True)
        width_fitting_nul = (
            version + "\x00" + "X" * 25
            if encoding == "UTF-8"
            else version[:-1] + "\x00"
        )
        async with database.transaction() as writer:
            await insert_legacy_anomalies(writer, selected, width_fitting_nul)

        await upgrade_database(settings)
        async with database.read_transaction() as connection:
            parameters = {"graph_id": selected, "deployment_digest": DIGEST}
            with executed_queries(connection) as statements:
                found, missing, blob, nul = [
                    (
                        await connection.execute(
                            SQLITE_DIGEST_MATCH,
                            parameters | {"deployment_digest": digest},
                        )
                    ).all()
                    for digest in (DIGEST, OTHER_DIGEST, BLOB_ID_DIGEST, NUL_ID_DIGEST)
                ]
            root: object = await connection.scalar(
                text("SELECT rootpage FROM sqlite_schema WHERE name = 'graph_version'")
            )
            await assert_sqlite_index(
                connection, DIGEST_INDEX, VALID_COLUMNS, SQLITE_VALID
            )
            await assert_sqlite_index(
                connection, INVALID_ID_INDEX, LOCATOR_COLUMNS, SQLITE_INVALID
            )
            opcodes = [
                await sqlite_instructions(connection, query, values)
                for query, values in statements
            ]
        async with database.transaction() as writer:
            await writer.execute(
                graph_version.delete().where(graph_version.c.number.in_((2, 3, 4)))
            )
            admitted = (await writer.execute(SQLITE_DIGEST_MATCH, parameters)).all()

        assert found == [(None,), (version,)]
        assert missing == []
        assert blob == nul == [(None,)]
        assert admitted == [(version,)]
        assert len(statements) == 4
        assert type(root) is int
        for program in opcodes:
            table_cursors = {
                row[2] for row in program if row[1] == "OpenRead" and row[3] == root
            }
            assert not any(
                row[2] in table_cursors
                and row[1]
                in {
                    "Column",
                    "FinishSeek",
                    "Next",
                    "NotExists",
                    "Rewind",
                    "RowData",
                    "Rowid",
                    "SeekGE",
                    "SeekGT",
                    "SeekLE",
                    "SeekLT",
                    "SeekRowid",
                }
                for row in program
            )
            assert (
                sum(row[1] in {"SeekGE", "SeekGT"} and row[5] == "2" for row in program)
                == 2
            )
    finally:
        await database.close()


async def wait_for_invalid_index(
    database: Database, index_name: str = DIGEST_INDEX
) -> int:
    async with asyncio.timeout(DEADLINE):
        while True:
            async with database.read_transaction() as connection:
                oid: object = await connection.scalar(
                    text(
                        "SELECT i.indexrelid FROM pg_index i JOIN pg_class c "
                        "ON c.oid = i.indexrelid "
                        "WHERE c.relname = :name AND NOT i.indisvalid"
                    ),
                    {"name": index_name},
                )
            if type(oid) is int:
                return oid


@pytest.mark.asyncio
async def test_graph_digest_locator_refuses_conflict_and_recovers_native_build(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    migrators: list[tuple[Database, ConnectionLedger]] = []

    def open_migrator(settings: DatabaseSettings) -> Database:
        migrator = Database(settings)
        migrators.append((migrator, ConnectionLedger(migrator)))
        return migrator

    try:
        binding, selected, version = await seed_predecessor(database)
        before = await history(database)
        conflicting_index = (
            INVALID_ID_INDEX if database.dialect is Dialect.SQLITE else DIGEST_INDEX
        )
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                f"CREATE INDEX {conflicting_index} ON graph_version (number)"
            )

        with pytest.raises(
            (RuntimeError, DBAPIError), match="incompatible|already exists"
        ):
            await upgrade_digest(empty_database_settings, open_migrator)
        async with database.transaction() as writer:
            refused_revision = await writer.run_sync(current_revision)
            if database.dialect is Dialect.SQLITE:
                locator_names = (
                    await writer.execute(
                        text(
                            "SELECT name FROM sqlite_schema WHERE type = 'index' "
                            "AND name IN (:valid, :invalid)"
                        ),
                        {"valid": DIGEST_INDEX, "invalid": INVALID_ID_INDEX},
                    )
                ).all()
                assert locator_names == [(conflicting_index,)]
            await writer.exec_driver_sql(f"DROP INDEX {conflicting_index}")
        invalid_oid: int | None = None
        if database.dialect is Dialect.POSTGRESQL:
            async with database.transaction() as writer:
                await writer.exec_driver_sql("UPDATE graph_version SET status = status")
                task = asyncio.create_task(
                    upgrade_digest(empty_database_settings, open_migrator)
                )
                try:
                    invalid_oid = await wait_for_invalid_index(database)
                finally:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            assert await migration_lock_count(database) == 0
            async with database.read_transaction() as connection:
                interrupted_revision = await connection.run_sync(current_revision)
                old_valid: object = await connection.scalar(
                    text("SELECT indisvalid FROM pg_index WHERE indexrelid = :oid"),
                    {"oid": invalid_oid},
                )
            assert interrupted_revision == PREDECESSOR
            assert old_valid is False
        report = await upgrade_digest(empty_database_settings, open_migrator)
        after = await history(database)
        found = await read_digest(database, binding, selected)

        assert refused_revision == PREDECESSOR
        assert report.from_revision == PREDECESSOR
        assert report.to_revision == REVISION
        assert before == after
        assert found == version
        assert await migration_lock_count(database) == 0
        assert database.units_in_flight() == 0
        assert len(migrators) == (2 if database.dialect is Dialect.SQLITE else 3)
        for migrator, ledger in migrators:
            assert migrator.units_in_flight() == ledger.outstanding == 0
            assert ledger.checkouts > 0
            assert ledger.opened == ledger.closed > 0
    finally:
        await database.close()
