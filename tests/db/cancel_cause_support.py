import asyncio
import json
from dataclasses import dataclass

from alembic import command
from sqlalchemy import Connection, MetaData, Table, event, inspect, select, text, update
from sqlalchemy.util import await_

from kora.db.connection import MIGRATION_LOCK_ID, Database, DatabaseSettings, Dialect
from kora.db.migrations.environment import alembic_config
from kora.db.tables import attempt
from tests.db.closing import ConnectionLedger
from tests.db.seed import (
    START,
    PredecessorHistory,
    Seeder,
    predecessor_snapshot,
    seed_predecessor,
)

VALIDATE_CAUSE: str = "ALTER TABLE attempt VALIDATE CONSTRAINT ck_attempt_cancel_cause"


@dataclass(frozen=True)
class CausePredecessor:
    history: PredecessorHistory
    attempt_table: Table
    old_insert: dict[str, object]
    schema_objects: tuple[str, str]


def attempt_schema_objects(connection: Connection) -> tuple[str, str]:
    inspector = inspect(connection)
    return (
        json.dumps(inspector.get_foreign_keys("attempt"), sort_keys=True, default=str),
        json.dumps(inspector.get_indexes("attempt"), sort_keys=True, default=str),
    )


def capture_cause_predecessor(connection: Connection) -> CausePredecessor:
    inspector = inspect(connection)
    columns = {
        table_name: tuple(
            str(column["name"]) for column in inspector.get_columns(table_name)
        )
        for table_name in inspector.get_table_names()
        if table_name != "alembic_version"
    }
    attempt_table = Table(
        "attempt", MetaData(), autoload_with=connection, resolve_fks=False
    )
    first = dict(connection.execute(select(attempt_table).limit(1)).mappings().one())
    return CausePredecessor(
        predecessor_snapshot(connection, columns),
        attempt_table,
        {**first, "id": "old-writer-after-cause", "number": 10003},
        attempt_schema_objects(connection),
    )


async def seed_attempts(database: Database) -> list[str]:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        version_id = await seeder.graph_version(graph_id)
        run_id = await seeder.run(namespace_id, graph_id, version_id)
        node_id = await seeder.node(run_id, "task")
        return [
            await seeder.attempt(node_id, status=status, number=number)
            for number, status in enumerate(
                ("created", "running", "finishing", "succeeded", "failed", "cancelled"),
                1,
            )
        ]


async def seed_cause_predecessor(
    database: Database, rich: bool = False
) -> CausePredecessor:
    if rich:
        async with database.transaction() as connection:
            await connection.run_sync(seed_predecessor)
    async with database.migration_connection() as connection:
        await connection.run_sync(
            lambda sync: command.upgrade(alembic_config(sync), "0003")
        )
    await seed_attempts(database)
    async with database.transaction() as connection:
        await connection.execute(
            update(attempt)
            .where(attempt.c.status == "finishing")
            .values(started_at=START, deadline=START, deadline_expired_at=START)
        )
        return await connection.run_sync(capture_cause_predecessor)


class UpgradeStage:
    def __init__(
        self,
        sqlite_after: bool = True,
        column_name: str = "cancel_cause",
        constraint_name: str = "ck_attempt_cancel_cause",
    ) -> None:
        self.sqlite_after: bool = sqlite_after
        self.column_name: str = column_name
        self.validation: str = (
            f"ALTER TABLE attempt VALIDATE CONSTRAINT {constraint_name}"
        )
        self.entered: asyncio.Event = asyncio.Event()
        self.release: asyncio.Event = asyncio.Event()
        self.database: Database | None = None
        self.ledger: ConnectionLedger | None = None
        self.preparation_locks: list[tuple[int, int]] = []

    def open(self, settings: DatabaseSettings) -> Database:
        database = Database(settings)
        self.database = database
        self.ledger = ConnectionLedger(database)
        event.listen(
            database.writer_engine.sync_engine, "before_cursor_execute", self.before
        )
        event.listen(
            database.writer_engine.sync_engine, "after_cursor_execute", self.after
        )
        return database

    def before(
        self,
        connection: Connection,
        cursor: object,
        statement: str,
        *arguments: object,
    ) -> None:
        if (
            connection.dialect.name == "sqlite"
            and not self.sqlite_after
            and statement.startswith(
                f"ALTER TABLE attempt ADD COLUMN {self.column_name}"
            )
        ):
            self.entered.set()
            await_(self.release.wait())
            return
        if connection.dialect.name != "postgresql" or statement != self.validation:
            return
        exclusive = int(
            connection.exec_driver_sql(
                "SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid() "
                "AND relation = 'attempt'::regclass AND mode = 'AccessExclusiveLock'"
            ).scalar_one()
        )
        lease = int(
            connection.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid() "
                    "AND locktype = 'advisory' AND objid = :key AND granted"
                ),
                {"key": MIGRATION_LOCK_ID},
            ).scalar_one()
        )
        self.preparation_locks.append((exclusive, lease))
        self.entered.set()
        await_(self.release.wait())

    def after(
        self,
        connection: Connection,
        cursor: object,
        statement: str,
        *arguments: object,
    ) -> None:
        if (
            self.sqlite_after
            and connection.dialect.name == "sqlite"
            and statement.startswith(
                f"ALTER TABLE attempt ADD COLUMN {self.column_name}"
            )
        ):
            self.entered.set()
            await_(self.release.wait())


async def wait_for_validation(
    database: Database, statement: str = VALIDATE_CAUSE
) -> None:
    async with asyncio.timeout(4):
        while True:
            async with database.read_transaction() as connection:
                waiting = await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                        "current_database() AND query = :query "
                        "AND wait_event_type = 'Lock'"
                    ),
                    {"query": statement},
                )
                if waiting.scalar_one() == 1:
                    return


async def migration_lock_count(database: Database) -> int:
    if database.dialect is Dialect.SQLITE:
        return 0
    async with database.read_transaction() as connection:
        return int(
            (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                        "AND objid = :key AND database = "
                        "(SELECT oid FROM pg_database "
                        "WHERE datname = current_database())"
                    ),
                    {"key": MIGRATION_LOCK_ID},
                )
            ).scalar_one()
        )
