import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Connection, Text, cast, event, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, DatabaseSettings
from kora.db.graph_preparation_reads import (
    GraphPreparationBinding,
    GraphPreparationSettlement,
    read_graph_preparation_settlement,
)
from kora.db.migrations import current_revision, schema_differences, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import (
    graph,
    graph_deployment_preparation,
    graph_version,
    idempotency_record,
    namespace,
)
from tests.db.cancel_cause_support import migration_lock_count
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, START, Seeder
from tests.support.databases import sqlite_settings
from tests.support.warning_filters import expression_index_warnings_ignored

RELATION: str = "graph_deployment_preparation"
PREDECESSOR: str = "0007"
REVISION: str = "0008"
MAX_NAME: str = "g" * 64
MAX_ULID: str = "7" + "Z" * 25


async def preparation_values(
    connection: AsyncConnection,
) -> dict[str, str | None]:
    seeder = Seeder(connection)
    namespace_id = await seeder.namespace()
    graph_id = await seeder.graph(namespace_id, MAX_NAME)
    version_id = await seeder.graph_version(graph_id)
    return {
        "id": MAX_ULID,
        "namespace_id": namespace_id,
        "graph_name": MAX_NAME,
        "initial_graph_id": None,
        "deployment_digest": "f" * 64,
        "state": "published",
        "result_graph_id": graph_id,
        "result_graph_version_id": version_id,
    }


@pytest.mark.asyncio
async def test_preparation_relation_preserves_all_approved_shapes(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        values = await preparation_values(connection)
    shapes = (
        values,
        {
            **values,
            "id": "0" * 26,
            "graph_name": "g",
            "initial_graph_id": values["result_graph_id"],
            "state": "reused",
        },
        {
            **values,
            "id": "1" * 26,
            "state": "open",
            "result_graph_id": None,
            "result_graph_version_id": None,
        },
        {
            **values,
            "id": "2" * 26,
            "state": "closed_without_publication",
            "result_graph_id": None,
            "result_graph_version_id": None,
        },
    )

    async with database.transaction() as connection:
        await connection.execute(graph_deployment_preparation.insert(), list(shapes))
    async with database.read_transaction() as connection:
        result = await connection.execute(select(graph_deployment_preparation))
        actual = {str(row["state"]): dict(row) for row in result.mappings()}

    assert actual == {str(row["state"]): row for row in shapes}


@pytest.mark.asyncio
async def test_preparation_native_checks_refuse_invalid_scope_and_shape(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        values = await preparation_values(connection)
        await connection.execute(
            namespace.insert().values(
                id="legacy-namespace",
                name="legacy",
                policy_revision=1,
                policy={},
                **Seeder(connection).stamps(),
            )
        )
    invalid: tuple[dict[str, str | None], ...] = (
        {"id": "8" + "0" * 25},
        {"id": "0" * 25 + "I"},
        {"id": MAX_ULID + " "},
        {"id": MAX_ULID.lower()},
        {"id": "0" * 25},
        {"namespace_id": "1" * 26},
        {"namespace_id": "legacy-namespace"},
        {"namespace_id": None},
        {"graph_name": "9invalid"},
        {"graph_name": "g-invalid"},
        {"graph_name": "gé"},
        {"graph_name": MAX_NAME + " "},
        {"deployment_digest": "A" * 64},
        {"deployment_digest": "0" * 64 + "  "},
        {"deployment_digest": "0" * 63},
        {"state": "Published"},
        {
            "state": "closed_without_publication ",
            "result_graph_id": None,
            "result_graph_version_id": None,
        },
        {"state": None},
        {"state": "open"},
        {"state": "closed_without_publication"},
        {"result_graph_id": None},
        {"result_graph_id": "0" * 25 + "U"},
        {"result_graph_version_id": None},
        {"result_graph_version_id": "Z" * 26},
        {"initial_graph_id": "0" * 26},
        {
            "initial_graph_id": "0" * 25 + "U",
            "state": "open",
            "result_graph_id": None,
            "result_graph_version_id": None,
        },
    )

    for replacement in invalid:
        with pytest.raises(IntegrityError):
            async with database.transaction() as connection:
                await connection.execute(
                    graph_deployment_preparation.insert().values(
                        **(values | replacement)
                    )
                )
    with pytest.raises(DBAPIError):
        async with database.transaction() as connection:
            await connection.execute(
                graph_deployment_preparation.insert()
                .values(**values)
                .values(graph_name="valid\0suffix")
            )
    async with database.transaction() as connection:
        await connection.execute(graph_deployment_preparation.insert().values(**values))
    with pytest.raises(IntegrityError):
        async with database.transaction() as connection:
            await connection.execute(
                graph_deployment_preparation.insert().values(**values)
            )
    async with database.read_transaction() as connection:
        result = await connection.execute(select(graph_deployment_preparation))

    assert dict(result.mappings().one()) == values


async def upgrade_predecessor(database: Database) -> None:
    async with database.migration_connection() as connection:
        await connection.run_sync(
            lambda sync: command.upgrade(alembic_config(sync), PREDECESSOR)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-16le", "utf-16be"])
async def test_preparation_migrates_native_sqlite_utf16_and_refuses_bad_text(
    tmp_path: Path, encoding: str
) -> None:
    path = tmp_path / "preparation.db"
    with closing(sqlite3.connect(path)) as native:
        native.execute(
            "PRAGMA encoding = 'UTF-16le'"
            if encoding == "utf-16le"
            else "PRAGMA encoding = 'UTF-16be'"
        )
        native.execute("CREATE TABLE encoding_seed (value INTEGER)")
        native.execute("DROP TABLE encoding_seed")
        native.commit()
    settings = sqlite_settings(path)
    database = Database(settings)
    try:
        await upgrade_predecessor(database)
        async with database.transaction() as connection:
            values = await preparation_values(connection)
        namespace_id = values["namespace_id"]
        assert namespace_id is not None
        binding = GraphPreparationBinding(
            MAX_ULID, namespace_id, MAX_NAME, None, "f" * 64
        )
        invalid = ("valid\0suffix", "valid\ud800")

        report = await upgrade_database(settings)
        for bad_name in invalid:
            encoded = bad_name.encode(encoding, errors="surrogatepass")
            with pytest.raises(IntegrityError):
                async with database.transaction() as connection:
                    await connection.execute(
                        graph_deployment_preparation.insert()
                        .values(**values)
                        .values(graph_name=cast(encoded, Text()))
                    )
        with pytest.raises(IntegrityError):
            async with database.transaction() as connection:
                await connection.execute(
                    graph_deployment_preparation.insert()
                    .values(**values)
                    .values(graph_name=MAX_NAME.encode(encoding))
                )
        async with database.transaction() as connection:
            await connection.execute(
                graph_deployment_preparation.insert().values(**values)
            )
        async with database.read_transaction() as connection:
            actual_encoding = await connection.scalar(text("PRAGMA encoding"))
            result = await connection.execute(select(graph_deployment_preparation))
            settlement = await read_graph_preparation_settlement(connection, binding)

        assert str(actual_encoding).lower() == encoding
        assert (report.from_revision, report.to_revision, report.steps) == (
            PREDECESSOR,
            "0021",
            14,
        )
        assert dict(result.mappings().one()) == values
        assert settlement == GraphPreparationSettlement(
            "published", values["result_graph_id"], values["result_graph_version_id"]
        )
    finally:
        await database.close()


class StampFailure(RuntimeError):
    pass


@dataclass
class MigrationInterruption:
    database: Database | None = None
    ledger: ConnectionLedger | None = None
    saw_stamp: bool = False

    def open(self, settings: DatabaseSettings) -> Database:
        database = Database(settings)
        self.database = database
        self.ledger = ConnectionLedger(database)
        event.listen(
            database.writer_engine.sync_engine, "after_cursor_execute", self.after
        )
        return database

    def after(
        self, connection: Connection, cursor: object, statement: str, *args: object
    ) -> None:
        if statement.lstrip().startswith("UPDATE alembic_version SET version_num="):
            self.saw_stamp = True
            raise StampFailure("refused preparation migration after stamp")


async def populated_predecessor(database: Database) -> str:
    await upgrade_predecessor(database)
    async with database.transaction() as connection:
        values = await preparation_values(connection)
        seeder = Seeder(connection)
        token_id = await seeder.token(DIGEST)
        await connection.execute(
            idempotency_record.insert().values(
                token_id=token_id,
                key="graph-deploy",
                request_digest=DIGEST,
                response={"graph_version_id": values["result_graph_version_id"]},
                expires_at=START,
                **seeder.stamps(),
            )
        )
        return str(values["namespace_id"])


async def history(database: Database) -> tuple[tuple[dict[str, object], ...], ...]:
    snapshots: list[tuple[dict[str, object], ...]] = []
    async with database.read_transaction() as connection:
        for relation in (graph, graph_version, idempotency_record):
            result = await connection.execute(select(relation))
            snapshots.append(tuple(dict(row) for row in result.mappings()))
    return tuple(snapshots)


def relation_schema(connection: Connection) -> tuple[set[str], set[str], list[str]]:
    inspector = inspect(connection)
    columns = {str(column["name"]) for column in inspector.get_columns(RELATION)}
    checks = {str(check["name"]) for check in inspector.get_check_constraints(RELATION)}
    with expression_index_warnings_ignored():
        differences = list(schema_differences(connection))
    return columns, checks, differences


@pytest.mark.asyncio
async def test_preparation_migration_rolls_back_stamp_and_releases_writer(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    interruption = MigrationInterruption()
    try:
        namespace_id = await populated_predecessor(database)
        before = await history(database)

        with pytest.raises(StampFailure, match="refused preparation migration"):
            await upgrade_database(empty_database_settings, interruption.open)
        async with database.read_transaction() as connection:
            rolled_back = await connection.run_sync(current_revision)
            absent = await connection.run_sync(
                lambda sync: not inspect(sync).has_table(RELATION)
            )
        locks = await migration_lock_count(database)
        fresh = Database(empty_database_settings)
        try:
            async with fresh.transaction() as connection:
                await connection.execute(
                    namespace.update()
                    .where(namespace.c.id == namespace_id)
                    .values(name=namespace.c.name)
                )
        finally:
            await fresh.close()
        recovered = await upgrade_database(empty_database_settings)
        repeated = await upgrade_database(empty_database_settings)
        async with database.read_transaction() as connection:
            columns, checks, differences = await connection.run_sync(relation_schema)
            stored = (
                await connection.execute(select(graph_deployment_preparation))
            ).all()
        after = await history(database)

        assert interruption.saw_stamp
        assert interruption.database is not None
        assert interruption.ledger is not None
        assert interruption.ledger.checkouts > 0
        assert (
            interruption.ledger.outstanding
            == interruption.database.units_in_flight()
            == locks
            == 0
        )
        assert rolled_back == PREDECESSOR
        assert absent
        assert (recovered.from_revision, recovered.to_revision, recovered.steps) == (
            PREDECESSOR,
            "0021",
            14,
        )
        assert repeated.steps == 0
        assert before == after
        assert all(len(rows) == 1 for rows in after)
        assert stored == []
        assert columns == {
            "id",
            "namespace_id",
            "graph_name",
            "initial_graph_id",
            "deployment_digest",
            "state",
            "result_graph_id",
            "result_graph_version_id",
        }
        assert checks == {
            f"ck_{RELATION}_{suffix}"
            for suffix in (
                "id_domain",
                "namespace_id_domain",
                "graph_name_domain",
                "initial_graph_id_domain",
                "deployment_digest_domain",
                "result_graph_id_domain",
                "result_graph_version_id_domain",
                "result_shape",
                "result_target",
            )
        }
        assert differences == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_preparation_migration_refuses_incompatible_existing_relation(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        await upgrade_predecessor(database)
        async with database.transaction() as connection:
            await connection.execute(
                text("CREATE TABLE graph_deployment_preparation (unrelated INTEGER)")
            )

        with pytest.raises(DBAPIError):
            await upgrade_database(empty_database_settings)
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            columns = await connection.run_sync(
                lambda sync: inspect(sync).get_columns(RELATION)
            )

        assert revision == PREDECESSOR
        assert [column["name"] for column in columns] == ["unrelated"]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_preparation_migration_downgrade_preserves_unsettled_row(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        async with database.migration_connection() as connection:
            await connection.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), REVISION)
            )
        async with database.transaction() as connection:
            values = await preparation_values(connection)
            open_values = {
                **values,
                "state": "open",
                "result_graph_id": None,
                "result_graph_version_id": None,
            }
            await connection.execute(
                graph_deployment_preparation.insert().values(**open_values)
            )

        with pytest.raises(
            RuntimeError, match="preparation history cannot be downgraded"
        ):
            async with database.migration_connection() as connection:
                await connection.run_sync(
                    lambda sync: command.downgrade(alembic_config(sync), PREDECESSOR)
                )
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            result = await connection.execute(select(graph_deployment_preparation))

        assert revision == REVISION
        assert dict(result.mappings().one()) == open_values
    finally:
        await database.close()
