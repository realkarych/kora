import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from sqlalchemy import Connection, event, insert, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.util.concurrency import await_

from kora.db.connection import Database, DatabaseClosedError, DatabaseSettings, Dialect
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import run, run_inputs
from tests.db.cancel_cause_support import migration_lock_count
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_db_upgrade_process import events, records, upgrade
from tests.db.test_migrations import differences_ignoring_expression_indexes
from tests.support.databases import sqlite_settings

PREDECESSOR = "0019"
REVISION = "0020"
HEAD_REVISION = "0022"
SENTINEL = "kora.run-inputs.v1"
ADD_INPUT_ENCODING = "ALTER TABLE run ADD COLUMN input_encoding_version INTEGER"
RUN_IDENTIFIER = re.compile(r"\b(?:run|run_inputs)\b", re.IGNORECASE)
PROJECTION = b'{"n":-0.0}'
PARAMETERS = '{"n":-0.0,"é":"\\u0000"}'.encode()


async def seed_predecessor(database: Database) -> tuple[str, str]:
    async with database.migration_connection() as connection:
        await connection.run_sync(
            lambda sync: command.upgrade(alembic_config(sync), PREDECESSOR)
        )
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace_id = await seed.namespace()
        graph_id = await seed.graph(namespace_id)
        version_id = await seed.graph_version(graph_id)
        legacy = seed.run_values(namespace_id, graph_id, version_id)
        legacy["params"] = SENTINEL
        legacy["cel_params_projection"] = {"legacy": [None, False, 3, "é"]}
        await writer.execute(insert(run).values(**legacy))
        second = await seed.run(namespace_id, graph_id, version_id)
    return str(legacy["id"]), second


def parent_columns(connection: Connection) -> tuple[str, ...]:
    return tuple(column["name"] for column in inspect(connection).get_columns("run"))


async def legacy_rows(database: Database) -> tuple[tuple[object, ...], ...]:
    async with database.read_transaction() as connection:
        rows = await connection.execute(
            select(
                run.c.id, run.c.params, run.c.cel_params_projection, run.c.status
            ).order_by(run.c.id)
        )
        return tuple(tuple(row) for row in rows)


async def parent_storage_identity(database: Database) -> object:
    async with database.read_transaction() as connection:
        if database.dialect is Dialect.SQLITE:
            return await connection.scalar(
                text("SELECT rootpage FROM sqlite_schema WHERE name = 'run'")
            )
        return await connection.scalar(text("SELECT pg_relation_filenode('run')"))


def assert_native_shape(connection: Connection) -> None:
    inspector = inspect(connection)
    columns = inspector.get_columns("run")
    tag = columns[-1]
    assert tag["name"] == "input_encoding_version"
    assert tag["type"].compile(dialect=connection.dialect) == "INTEGER"
    assert tag["nullable"] is True
    assert tag["default"] is None
    children = inspector.get_columns("run_inputs")
    assert [column["name"] for column in children] == [
        "run_id",
        "cel_projection",
        "params",
    ]
    assert all(
        not column["nullable"] and column["default"] is None for column in children
    )
    expected_type = "BLOB" if connection.dialect.name == "sqlite" else "BYTEA"
    assert [
        column["type"].compile(dialect=connection.dialect) for column in children[1:]
    ] == [expected_type, expected_type]
    assert inspector.get_pk_constraint("run_inputs")["constrained_columns"] == [
        "run_id"
    ]
    (foreign_key,) = inspector.get_foreign_keys("run_inputs")
    assert foreign_key["constrained_columns"] == ["run_id"]
    assert foreign_key["referred_table"] == "run"
    assert foreign_key["referred_columns"] == ["id"]
    assert foreign_key.get("options", {}) == {}
    if connection.dialect.name == "sqlite":
        collation = connection.execute(
            text(
                "SELECT x.coll FROM pragma_index_list('run_inputs') AS i "
                "JOIN pragma_index_xinfo(i.name) AS x WHERE i.origin = 'pk' AND x.key"
            )
        ).scalar_one()
        assert collation == "BINARY"
        (check,) = inspector.get_check_constraints("run_inputs")
        assert check["name"] == "ck_run_inputs_binary_storage"
        assert check["sqltext"] == (
            "typeof(cel_projection) = 'blob' AND typeof(params) = 'blob'"
        )
    else:
        collation = connection.execute(
            text(
                "SELECT c.collname FROM pg_attribute AS a "
                "JOIN pg_collation AS c ON c.oid = a.attcollation "
                "WHERE a.attrelid = 'run_inputs'::regclass AND a.attname = 'run_id'"
            )
        ).scalar_one()
        assert collation == "C"
        assert inspector.get_check_constraints("run_inputs") == []


@dataclass
class UpgradeObservation:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event | None = None
    statements: list[str] = field(default_factory=list[str])
    database: Database | None = None
    ledger: ConnectionLedger | None = None

    def open(self, settings: DatabaseSettings) -> Database:
        database = Database(settings)
        self.database = database
        self.ledger = ConnectionLedger(database)
        event.listen(
            database.writer_engine.sync_engine, "after_cursor_execute", self.after
        )
        return database

    def after(
        self, connection: Connection, cursor: object, statement: str, *arguments: object
    ) -> None:
        normal = " ".join(statement.split())
        self.statements.append(normal)
        if normal.startswith("CREATE TABLE run_inputs "):
            self.entered.set()
            if self.release is not None:
                await_(self.release.wait())

    def assert_released(self) -> None:
        assert self.database is not None
        assert self.ledger is not None
        assert self.database.units_in_flight() == 0
        assert self.ledger.outstanding == self.ledger.closed_while_checked_out == 0
        assert self.ledger.opened == self.ledger.closed > 0


@pytest.mark.asyncio
async def test_run_input_migration_adds_only_empty_native_storage(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    observation = UpgradeObservation()
    try:
        legacy_id, _ = await seed_predecessor(database)
        before = await legacy_rows(database)
        storage_before = await parent_storage_identity(database)
        async with database.read_transaction() as reader:
            columns_before = await reader.run_sync(parent_columns)

        report = await upgrade_database(empty_database_settings, observation.open)
        repeated = await upgrade_database(empty_database_settings)
        after = await legacy_rows(database)
        storage_after = await parent_storage_identity(database)
        async with database.read_transaction() as reader:
            columns_after = await reader.run_sync(parent_columns)
            await reader.run_sync(assert_native_shape)
            schema_differences = await reader.run_sync(
                differences_ignoring_expression_indexes
            )
            tags = (
                (await reader.execute(select(run.c.input_encoding_version)))
                .scalars()
                .all()
            )
            side_rows = (await reader.execute(select(run_inputs))).all()

        assert (report.from_revision, report.to_revision, report.steps) == (
            PREDECESSOR,
            HEAD_REVISION,
            3,
        )
        assert (repeated.from_revision, repeated.to_revision, repeated.steps) == (
            HEAD_REVISION,
            HEAD_REVISION,
            0,
        )
        assert after == before
        assert (
            legacy_id,
            SENTINEL,
            {"legacy": [None, False, 3, "é"]},
            "running",
        ) in after
        assert storage_before == storage_after
        assert storage_after is not None
        assert columns_after == (*columns_before, "input_encoding_version")
        assert tags == [None, None]
        assert side_rows == []
        assert schema_differences == []
        scoped = [
            statement
            for statement in observation.statements
            if RUN_IDENTIFIER.search(statement) is not None
        ]
        assert scoped[0] == ADD_INPUT_ENCODING
        assert scoped[1].startswith("CREATE TABLE run_inputs ")
        expected_index_prefixes = (
            [
                "CREATE INDEX ix_run_input_locator",
                "CREATE INDEX ix_run_inputs_locator",
                "CREATE INDEX ix_run_status_id",
            ]
            if database.dialect is Dialect.SQLITE
            else ["CREATE INDEX CONCURRENTLY ix_run_status_id"]
        )
        assert scoped[-1].startswith("CREATE TABLE run_promotion_admission (")
        index_statements = scoped[2:-1]
        if database.dialect is Dialect.POSTGRESQL:
            assert index_statements[0] == (
                "SELECT indisvalid, indrelid = 'run'::regclass, indnkeyatts, "
                "pg_get_indexdef(indexrelid, 1, true), "
                "pg_get_indexdef(indexrelid, 2, true), indpred IS NULL FROM pg_index "
                "WHERE indexrelid = to_regclass('ix_run_status_id')"
            )
            index_statements = index_statements[1:]
        assert [statement.split(" ON ", 1)[0] for statement in index_statements] == (
            expected_index_prefixes
        )
        observation.assert_released()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_run_input_migration_preserves_native_bytes_and_refuses_downgrade(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    downgrade = UpgradeObservation()
    try:
        _, run_id = await seed_predecessor(database)
        async with database.migration_connection() as connection:
            await connection.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), REVISION)
            )
        values = {"run_id": run_id, "cel_projection": PROJECTION, "params": PARAMETERS}
        async with database.transaction() as writer:
            await writer.execute(
                run.update()
                .where(run.c.id == run_id)
                .values(
                    input_encoding_version=1,
                    params=SENTINEL,
                    cel_params_projection=SENTINEL,
                )
            )
            await writer.execute(insert(run_inputs).values(**values))
        before = await legacy_rows(database)

        with pytest.raises(IntegrityError):
            async with database.transaction() as writer:
                await writer.execute(insert(run_inputs).values(**values))
        with pytest.raises(IntegrityError):
            async with database.transaction() as writer:
                await writer.execute(
                    insert(run_inputs).values(**{**values, "run_id": run_id.lower()})
                )
        with pytest.raises(IntegrityError):
            async with database.transaction() as writer:
                await writer.execute(run.delete().where(run.c.id == run_id))
        migrator = downgrade.open(empty_database_settings)
        try:
            with pytest.raises(
                RuntimeError,
                match=(
                    "^run input history cannot be downgraded; "
                    "restore a compatible backup$"
                ),
            ):
                async with migrator.migration_connection() as connection:
                    await connection.run_sync(
                        lambda sync: command.downgrade(
                            alembic_config(sync), PREDECESSOR
                        )
                    )
        finally:
            await migrator.close()
        async with database.transaction() as independent_writer:
            await Seeder(independent_writer).namespace("downgrade-writer-probe")
        after = await legacy_rows(database)
        async with database.read_transaction() as reader:
            revision = await reader.run_sync(current_revision)
            await reader.run_sync(assert_native_shape)
            side_rows = (await reader.execute(select(run_inputs))).all()
            stored_tag: object = await reader.scalar(
                select(run.c.input_encoding_version).where(run.c.id == run_id)
            )

        assert stored_tag == 1
        assert run_id.lower() != run_id
        assert revision == REVISION
        assert before == after
        assert side_rows == [(run_id, PROJECTION, PARAMETERS)]
        assert await migration_lock_count(database) == 0
        downgrade.assert_released()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_sqlite_run_input_storage_refuses_text_in_each_binary_column(
    tmp_path: Path,
) -> None:
    settings = sqlite_settings(tmp_path / "empty.db")
    database = Database(settings)
    try:
        _, run_id = await seed_predecessor(database)
        await upgrade_database(settings)

        for projection, params in (("{}", b"{}"), (b"{}", "{}")):
            with pytest.raises(IntegrityError, match="ck_run_inputs_binary_storage"):
                async with database.transaction() as writer:
                    await writer.execute(
                        text(
                            "INSERT INTO run_inputs VALUES (:id, :projection, :params)"
                        ),
                        {"id": run_id, "projection": projection, "params": params},
                    )
        async with database.read_transaction() as reader:
            rows = (await reader.execute(select(run_inputs))).all()

        assert rows == []
    finally:
        await database.close()


def input_schema(connection: Connection) -> tuple[tuple[str, ...], tuple[str, ...]]:
    inspector = inspect(connection)
    child = (
        tuple(column["name"] for column in inspector.get_columns("run_inputs"))
        if inspector.has_table("run_inputs")
        else ()
    )
    return parent_columns(connection), child


@pytest.mark.asyncio
async def test_run_input_migration_conflict_is_atomic_and_content_free(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    marker = "PRIVATE-RUN-INPUT-MIGRATION-CANARY"
    observation = UpgradeObservation()
    try:
        await seed_predecessor(database)
        before = await legacy_rows(database)
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "CREATE TABLE run_inputs (conflict_marker TEXT NOT NULL)"
            )
            await writer.execute(
                text("INSERT INTO run_inputs (conflict_marker) VALUES (:marker)"),
                {"marker": marker},
            )
            schema_before = await writer.run_sync(input_schema)
        environment = {
            "KORA_DATABASE_URL": empty_database_settings.url.render_as_string(
                hide_password=False
            )
        }

        with pytest.raises(DBAPIError, match="already exists"):
            await upgrade_database(empty_database_settings, observation.open)
        failed = await asyncio.to_thread(upgrade, environment)
        after = await legacy_rows(database)
        locks = await migration_lock_count(database)
        async with database.transaction() as independent_writer:
            revision = await independent_writer.run_sync(current_revision)
            schema_after = await independent_writer.run_sync(input_schema)
            conflict_value: object = await independent_writer.scalar(
                text("SELECT conflict_marker FROM run_inputs")
            )
            await independent_writer.exec_driver_sql("DROP TABLE run_inputs")
        recovered = await asyncio.to_thread(upgrade, environment)
        async with database.read_transaction() as reader:
            await reader.run_sync(assert_native_shape)
            recovered_revision = await reader.run_sync(current_revision)
        failure_text = failed.stderr.decode()
        migration_failure = next(
            record
            for record in records(failed)
            if record["event"] == "db.migration.failed"
        )
        chain = cast(list[dict[str, object]], migration_failure["exception"])

        assert revision == PREDECESSOR
        assert schema_before == schema_after
        assert "input_encoding_version" not in schema_after[0]
        assert schema_after[1] == ("conflict_marker",)
        assert conflict_value == marker
        assert before == after
        assert locks == 0
        assert ADD_INPUT_ENCODING in observation.statements
        observation.assert_released()
        assert failed.code == 70
        assert failed.stdout == b""
        assert events(failed).count("db.migration.failed") == 1
        assert "db.migration.finished" not in events(failed)
        assert migration_failure["message"] == "migration failed"
        assert chain
        assert all(set(entry) <= {"type", "module", "frames"} for entry in chain)
        assert all(
            private not in failure_text
            for private in (
                marker,
                "Traceback",
                "CREATE TABLE",
                "ALTER TABLE",
                "conflict_marker",
                "run_inputs",
                "already exists",
            )
        )
        assert recovered.code == 0
        assert recovered.stdout == b"0022\n"
        assert recovered_revision == HEAD_REVISION
        assert events(recovered).count("db.migration.finished") == 1
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "shutdown"])
async def test_run_input_migration_interruption_releases_native_resources(
    empty_database_settings: DatabaseSettings, ending: str
) -> None:
    database = Database(empty_database_settings)
    release = asyncio.Event()
    observation = UpgradeObservation(release=release)
    try:
        await seed_predecessor(database)
        before = await legacy_rows(database)
        async with database.read_transaction() as reader:
            schema_before = await reader.run_sync(input_schema)

        task = asyncio.create_task(
            upgrade_database(empty_database_settings, observation.open)
        )
        try:
            await asyncio.wait_for(observation.entered.wait(), 4)
            if ending == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert observation.database is not None
                await observation.database.close(deadline_seconds=0)
                release.set()
                with pytest.raises(DatabaseClosedError):
                    await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        after = await legacy_rows(database)
        locks = await migration_lock_count(database)
        async with database.transaction() as independent_writer:
            revision = await independent_writer.run_sync(current_revision)
            schema_after = await independent_writer.run_sync(input_schema)
            await Seeder(independent_writer).namespace("migration-writer-probe")
        recovered = await upgrade_database(empty_database_settings)

        assert revision == PREDECESSOR
        assert schema_before == schema_after
        assert "input_encoding_version" not in schema_after[0]
        assert schema_after[1] == ()
        assert before == after
        assert locks == 0
        assert (recovered.from_revision, recovered.to_revision, recovered.steps) == (
            PREDECESSOR,
            HEAD_REVISION,
            3,
        )
        observation.assert_released()
    finally:
        await database.close()
