import asyncio
from dataclasses import dataclass

import pytest
from alembic import command
from sqlalchemy import Connection, Text, cast, event, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.util.concurrency import await_

from kora.db.connection import Database, DatabaseSettings, Dialect, Lifecycle
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.reads import run_by_id, target_holders
from kora.db.tables import run
from tests.db.cancel_cause_support import migration_lock_count
from tests.db.closing import CONDITION_DEADLINE_SECONDS, ConnectionLedger
from tests.db.seed import START, Seeder

PREDECESSOR = "0017"
REVISION = "0018"
HEAD_REVISION = "0019"
ORIGINAL = "R" * 255
UPPER = "A" * 256
LOWER = "a" * 256


@dataclass(frozen=True)
class RunScope:
    namespace_id: str
    graph_id: str
    version_id: str


def prepare(connection: Connection) -> None:
    command.upgrade(alembic_config(connection), PREDECESSOR)


def upgrade_width(connection: Connection) -> None:
    command.upgrade(alembic_config(connection), REVISION)


def downgrade(connection: Connection) -> None:
    command.downgrade(alembic_config(connection), PREDECESSOR)


def target_index(connection: Connection) -> tuple[tuple[str | None, ...], bool]:
    if connection.dialect.name == Dialect.SQLITE:
        with connection.execute(
            text(
                'SELECT columns.name, indexes."unique" '
                "FROM pragma_index_list('run') AS indexes "
                "CROSS JOIN pragma_index_info('ix_run_namespace_target') AS columns "
                "WHERE indexes.name = 'ix_run_namespace_target' ORDER BY columns.seqno"
            )
        ) as result:
            rows = result.all()
        names: list[str | None] = []
        flags: set[bool] = set()
        for name, unique in rows:
            assert name is None or type(name) is str
            assert type(unique) is int
            assert unique in (0, 1)
            names.append(name)
            flags.add(bool(unique))
        (is_unique,) = flags
        return tuple(names), is_unique
    (index,) = (
        index
        for index in inspect(connection).get_indexes("run")
        if index["name"] == "ix_run_namespace_target"
    )
    return tuple(index["column_names"]), index["unique"]


async def add_run(database: Database, scope: RunScope, key: str) -> str:
    async with database.transaction() as writer:
        return await Seeder(writer).run(
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            target_key=key,
            target_held_since=START,
        )


async def seed(database: Database) -> tuple[RunScope, str]:
    async with database.migration_connection() as migration:
        await migration.run_sync(prepare)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        version = await seeder.graph_version(graph)
    scope = RunScope(namespace, graph, version)
    return scope, await add_run(database, scope, ORIGINAL)


@dataclass(frozen=True)
class State:
    revision: str | None
    column: tuple[object, ...]
    index: tuple[tuple[str | None, ...], bool]
    keys: tuple[object, ...]
    original_key: str | None
    original_holder_count: int


async def state(database: Database, scope: RunScope, original_id: str) -> State:
    async with database.read_transaction() as reader:
        revision = await reader.run_sync(current_revision)
        native = (
            text(
                "SELECT character_maximum_length, collation_name, is_nullable "
                "FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name='run' AND column_name='target_key'"
            )
            if database.dialect is Dialect.POSTGRESQL
            else text(
                "SELECT type, \"notnull\" FROM pragma_table_info('run') "
                "WHERE name='target_key'"
            )
        )
        column = tuple((await reader.execute(native)).one())
        index = await reader.run_sync(target_index)
        result = await reader.scalars(
            select(cast(run.c.target_key, Text))
            .where(run.c.namespace_id == scope.namespace_id)
            .order_by(run.c.target_key)
        )
        keys = tuple(result)
        original = await run_by_id(reader, scope.namespace_id, original_id)
        assert original is not None
        holders = await target_holders(reader, scope.namespace_id, ORIGINAL)
        return State(revision, column, index, keys, original.target_key, len(holders))


@pytest.mark.asyncio
async def test_target_width_migration_preserves_order_and_existing_keys(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        scope, original_id = await seed(database)
        before = await state(database, scope, original_id)

        async with database.migration_connection() as migration:
            await migration.run_sync(upgrade_width)
        widened = await state(database, scope, original_id)
        async with database.migration_connection() as migration:
            with pytest.raises(
                RuntimeError,
                match=(
                    "^run target key migration cannot be downgraded; "
                    "restore a compatible backup$"
                ),
            ):
                await migration.run_sync(downgrade)
        report = await upgrade_database(empty_database_settings)
        upgraded = await state(database, scope, original_id)
        lower_id = await add_run(database, scope, LOWER)
        upper_id = await add_run(database, scope, UPPER)
        async with database.read_transaction() as reader:
            lower = await target_holders(reader, scope.namespace_id, LOWER)
            upper = await target_holders(reader, scope.namespace_id, UPPER)
        if database.dialect is Dialect.POSTGRESQL:
            with pytest.raises(DBAPIError) as over:
                await add_run(database, scope, "x" * 257)
            overflow_state = getattr(over.value.orig, "sqlstate", None)
        else:
            overflow_state = None
        after = await state(database, scope, original_id)
        repeated = await upgrade_database(empty_database_settings)
    finally:
        await database.close()

    assert before.revision == PREDECESSOR
    assert widened.revision == REVISION
    assert (report.from_revision, report.to_revision, report.steps) == (
        REVISION,
        HEAD_REVISION,
        1,
    )
    assert (repeated.from_revision, repeated.to_revision, repeated.steps) == (
        HEAD_REVISION,
        HEAD_REVISION,
        0,
    )
    assert after.revision == upgraded.revision == HEAD_REVISION
    assert before.keys == widened.keys == upgraded.keys == (ORIGINAL,)
    assert after.keys == (UPPER, ORIGINAL, LOWER)
    assert (
        before.original_key == upgraded.original_key == after.original_key == ORIGINAL
    )
    assert before.original_holder_count == upgraded.original_holder_count == 1
    assert [holder.run_id for holder in lower] == [lower_id]
    assert [holder.run_id for holder in upper] == [upper_id]
    assert (
        before.index
        == widened.index
        == upgraded.index
        == after.index
        == (("namespace_id", "target_key"), False)
    )
    if database.dialect is Dialect.POSTGRESQL:
        assert before.column == (255, "C", "YES")
        assert widened.column == upgraded.column == after.column == (256, "C", "YES")
        assert overflow_state == "22001"
    else:
        assert before.column == widened.column == upgraded.column == after.column
        assert after.column == ("VARCHAR(255)", 0)
    assert database.units_in_flight() == 0


class StampPause:
    def __init__(self) -> None:
        self.entered: asyncio.Event = asyncio.Event()
        self.release: asyncio.Event = asyncio.Event()
        self.database: Database | None = None
        self.ledger: ConnectionLedger | None = None
        self.altered: bool = False

    def open(self, settings: DatabaseSettings) -> Database:
        database = Database(settings)
        self.database = database
        self.ledger = ConnectionLedger(database)
        event.listen(
            database.writer_engine.sync_engine, "before_cursor_execute", self.before
        )
        return database

    def before(
        self,
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if statement.startswith("ALTER TABLE run ALTER COLUMN target_key"):
            self.altered = True
        if statement.startswith("UPDATE alembic_version") and REVISION in statement:
            self.entered.set()
            await_(self.release.wait())


@pytest.mark.asyncio
async def test_target_width_migration_cancellation_rolls_back_and_releases(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    pause = StampPause()
    try:
        scope, original_id = await seed(database)
        before = await state(database, scope, original_id)

        pending = asyncio.create_task(
            upgrade_database(empty_database_settings, pause.open)
        )
        try:
            await asyncio.wait_for(pause.entered.wait(), CONDITION_DEADLINE_SECONDS)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, CONDITION_DEADLINE_SECONDS)
        finally:
            pause.release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        after = await state(database, scope, original_id)
        locks = await migration_lock_count(database)
        recovered = await upgrade_database(empty_database_settings)
        await add_run(database, scope, UPPER)
        ready = await state(database, scope, original_id)
    finally:
        await database.close()

    assert pause.altered is (database.dialect is Dialect.POSTGRESQL)
    assert after == before
    assert locks == 0
    assert pause.database is not None
    assert pause.ledger is not None
    assert pause.database.lifecycle is Lifecycle.CLOSED
    assert pause.ledger.outstanding == pause.database.units_in_flight() == 0
    assert pause.ledger.opened == pause.ledger.closed > 0
    assert pause.ledger.closed_while_checked_out == 0
    assert recovered.from_revision == PREDECESSOR
    assert recovered.to_revision == ready.revision == HEAD_REVISION
    assert ready.keys == (UPPER, ORIGINAL)
