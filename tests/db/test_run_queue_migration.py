import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Connection, String, literal_column, select, text
from sqlalchemy.exc import DBAPIError

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.run_queue_index_v1 import (
    POSTGRES_QUEUE_INDEX,
    SQLITE_QUEUE_INDEX,
    SQLITE_QUEUE_KEY,
    SQLITE_QUEUE_ROWS,
)
from kora.db.tables import run
from kora.engine.run_queue import QueueState
from tests.db.cancel_cause_support import migration_lock_count
from tests.db.closing import ConnectionLedger
from tests.db.test_graph_publication_digest_migration import wait_for_invalid_index
from tests.db.test_graph_read_admission import executed_queries, sqlite_instructions
from tests.engine.test_run_queue import acquire, queue_case
from tests.support.databases import sqlite_settings

PREDECESSOR = "0018"
HEAD_REVISION = "0020"
WIDE = 4 * 1024 * 1024


def _predecessor(connection: Connection) -> None:
    command.upgrade(alembic_config(connection), PREDECESSOR)


async def _history(database: Database) -> tuple[tuple[object, ...], ...]:
    legacy_fields = tuple(
        column for column in run.c if column.name != "input_encoding_version"
    )
    async with database.read_transaction() as connection:
        with await connection.execute(
            select(*legacy_fields).order_by(run.c.id)
        ) as result:
            return tuple(tuple(row) for row in result)


@pytest.mark.asyncio
async def test_queue_index_upgrade_recovers_and_preserves_existing_history(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    ledger = ConnectionLedger(database)
    migrations: list[tuple[Database, ConnectionLedger]] = []
    postgres = database.dialect is Dialect.POSTGRESQL
    index = POSTGRES_QUEUE_INDEX if postgres else SQLITE_QUEUE_INDEX

    def open_migrator(settings: DatabaseSettings) -> Database:
        migrator = Database(settings)
        migrations.append((migrator, ConnectionLedger(migrator)))
        return migrator

    try:
        async with database.migration_connection() as connection:
            await connection.run_sync(_predecessor)
        case = await queue_case(database)
        before = await _history(database)
        async with database.transaction() as writer:
            await writer.exec_driver_sql(f"CREATE INDEX {index} ON run (status)")

        with pytest.raises(
            (RuntimeError, DBAPIError), match="incompatible|already exists"
        ):
            await upgrade_database(empty_database_settings, open_migrator)
        async with database.transaction() as writer:
            refused_revision = await writer.run_sync(current_revision)
            await writer.exec_driver_sql(f"DROP INDEX {index}")
        invalid: object = None
        if postgres:
            async with database.transaction() as writer:
                await writer.exec_driver_sql("UPDATE run SET status = status")
                task = asyncio.create_task(
                    upgrade_database(empty_database_settings, open_migrator)
                )
                try:
                    invalid_oid = await wait_for_invalid_index(database, index)
                finally:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            async with database.read_transaction() as reader:
                invalid = await reader.scalar(
                    text("SELECT indisvalid FROM pg_index WHERE indexrelid = :oid"),
                    {"oid": invalid_oid},
                )
        report = await upgrade_database(empty_database_settings, open_migrator)
        after = await _history(database)
        async with database.transaction() as writer:
            state = await case.observe(writer, await acquire(writer, case))

        assert refused_revision == PREDECESSOR
        assert invalid is (False if postgres else None)
        assert (report.from_revision, report.to_revision, report.steps) == (
            PREDECESSOR,
            HEAD_REVISION,
            2,
        )
        assert state is QueueState.OLDER_PENDING
        assert before == after
        assert await migration_lock_count(database) == 0
        assert database.units_in_flight() == ledger.outstanding == 0
        assert len(migrations) == (3 if postgres else 2)
        for migrator, opened in migrations:
            assert migrator.units_in_flight() == opened.outstanding == 0
            assert opened.opened == opened.closed > 0
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
async def test_sqlite_queue_index_keeps_wide_legacy_ids_out_of_the_ordered_key(
    tmp_path: Path, encoding: str
) -> None:
    path = tmp_path / "queue.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"PRAGMA encoding = '{encoding}'")
        connection.execute("CREATE TABLE encoding_seed (value INTEGER)")
        connection.execute("DROP TABLE encoding_seed")
    settings = sqlite_settings(path)
    database = Database(settings)
    try:
        async with database.migration_connection() as connection:
            await connection.run_sync(_predecessor)
        case = await queue_case(database)
        wide_text = "0" * (WIDE if encoding == "UTF-8" else WIDE // 2)
        async with database.transaction() as writer:
            await writer.execute(
                text("UPDATE run SET id = zeroblob(:width) WHERE id = :id"),
                {"width": WIDE, "id": case.oldest},
            )
            await writer.execute(
                text("UPDATE run SET id = :wide WHERE id = :id"),
                {"wide": wide_text, "id": case.earlier},
            )
        key = literal_column(f"({SQLITE_QUEUE_KEY})", String())
        query = (
            select(key)
            .select_from(text(f"main.run INDEXED BY {SQLITE_QUEUE_INDEX}"))
            .where(
                text("namespace_id = :namespace AND graph_id = :graph"),
                literal_column(SQLITE_QUEUE_ROWS),
                literal_column("status = 'pending'"),
            )
            .order_by(key)
        )
        parameters = {"namespace": case.namespace, "graph": case.graph}

        await upgrade_database(settings)
        async with database.transaction() as writer:
            state = await case.observe(writer, await acquire(writer, case))
            with executed_queries(writer) as statements:
                with await writer.execute(query, parameters) as cursor:
                    keys = cursor.scalars().all()
            [(statement, bindings)] = statements
            program = await sqlite_instructions(writer, statement, bindings)
            root = await writer.scalar(
                text("SELECT rootpage FROM main.sqlite_schema WHERE name = 'run'")
            )
            with await writer.execute(
                text(
                    "SELECT typeof(id), length(CAST(id AS BLOB)) FROM run "
                    "WHERE length(CAST(id AS BLOB)) = :width ORDER BY typeof(id)"
                ),
                {"width": WIDE},
            ) as native:
                widths = native.all()
        table = {row[2] for row in program if row[1] == "OpenRead" and row[3] == root}

        assert state is QueueState.INVALID_ORDER
        assert keys == ["", "", case.candidate, case.later]
        assert widths == [("blob", WIDE), ("text", WIDE)]
        assert all(
            row[2] not in table
            for row in program
            if row[1] in {"Column", "RowData", "Rowid", "SeekRowid", "NotExists"}
        )
        assert database.units_in_flight() == 0
    finally:
        await database.close()
