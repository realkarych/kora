import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from aiosqlite import Connection as SQLiteDriver
from aiosqlite import Cursor as SQLiteCursor
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.conformance.capture import captured
from kora.db import DB_COMPONENT
from kora.db.connection import Database, Dialect
from kora.db.migrations import upgrade_database
from kora.db.sqlite_run_input_layout import observe_sqlite_run_input_layout
from kora.observability import Level, LoggingSettings
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    ConnectionLedger,
    narrow_settings,
)
from tests.db.seed import FakeClock
from tests.db.test_graph_read_admission import executed_queries
from tests.db.test_sqlite_writer_entry import writer_driver
from tests.support.databases import sqlite_settings

INPUT_INDEX = "ix_run_inputs_locator"
INPUT_INDEX_DEFINITION = text(
    "SELECT sql FROM main.sqlite_schema WHERE type = 'index' AND name = :name"
)
DROP_INPUT_INDEX = "DROP INDEX main.ix_run_inputs_locator"
INPUT_TABLE_BODY = (
    "(run_id VARCHAR(26) NOT NULL PRIMARY KEY, "
    "cel_projection BLOB NOT NULL, params BLOB NOT NULL)"
)
TABLE_XINFO = (
    "PRAGMA main.table_xinfo(run)",
    "PRAGMA main.table_xinfo(run_inputs)",
)
INDEX_XINFO = (
    "PRAGMA main.index_xinfo(ix_run_input_locator)",
    "PRAGMA main.index_xinfo(ix_run_inputs_locator)",
)


async def open_sqlite(tmp_path: Path, encoding: str = "UTF-8") -> Database:
    settings = sqlite_settings(tmp_path / "catalog.db")
    with closing(sqlite3.connect(settings.sqlite_path)) as native:
        native.execute(f"PRAGMA encoding = '{encoding}'")
        native.execute("CREATE TABLE encoding_seed (value INTEGER)")
        native.execute("DROP TABLE encoding_seed")
        native.commit()
    await upgrade_database(settings)
    return Database(narrow_settings(settings, pool_size=1))


async def input_index_definition(connection: AsyncConnection) -> str:
    definition: object = await connection.scalar(
        INPUT_INDEX_DEFINITION, {"name": INPUT_INDEX}
    )
    assert type(definition) is str
    return definition


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
async def test_run_input_layout_checks_only_the_exact_main_catalog(
    tmp_path: Path, encoding: str
) -> None:
    database = await open_sqlite(tmp_path, encoding)
    ledger = ConnectionLedger(database)
    forbidden: list[tuple[str | None, str | None]] = []

    def deny_rows(
        action: int,
        name: str | None,
        column: str | None,
        schema: str | None,
        origin: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and name in ("run", "run_inputs"):
            forbidden.append((name, column))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    try:
        async with database.read_snapshot() as reader:
            driver = await writer_driver(reader)
            native_encoding: object = await reader.scalar(text("PRAGMA main.encoding"))
            await driver.set_authorizer(deny_rows)
            try:
                with executed_queries(reader) as queries:
                    observed = await observe_sqlite_run_input_layout(reader)
            finally:
                await driver.set_authorizer(None)
        statements = [statement.strip() for statement, _ in queries]

        assert observed is True
        assert native_encoding == encoding
        assert forbidden == []
        assert statements[:2] == ["PRAGMA query_only", "PRAGMA main.encoding"]
        assert statements[2].startswith("WITH expected(name, kind, owner, definition)")
        assert "JOIN main.sqlite_schema AS native" in statements[2]
        assert statements[3:] == [*TABLE_XINFO, *INDEX_XINFO]
        assert ledger.outstanding == database.units_in_flight() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_run_input_layout_requires_the_native_read_snapshot(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    unused = database.reader_engine.connect()
    unused_result = await observe_sqlite_run_input_layout(unused)
    unused_checkouts = ledger.checkouts
    async with database.read_transaction() as ordinary:
        with executed_queries(ordinary) as ordinary_queries:
            ordinary_result = await observe_sqlite_run_input_layout(ordinary)
    async with database.transaction() as writer:
        writer_result = await observe_sqlite_run_input_layout(writer)

    async with database.read_snapshot() as reader:
        with executed_queries(reader) as snapshot_queries:
            snapshot_result = await observe_sqlite_run_input_layout(reader)
        async with reader.begin_nested():
            with executed_queries(reader) as nested_queries:
                nested_result = await observe_sqlite_run_input_layout(reader)

    assert unused_result is ordinary_result is writer_result is nested_result is False
    assert unused_checkouts == 0
    assert ordinary_queries == nested_queries == []
    assert snapshot_result is (database.dialect is Dialect.SQLITE)
    if database.dialect is Dialect.POSTGRESQL:
        assert snapshot_queries == []
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        "CREATE INDEX ix_run_inputs_locator ON run_inputs(params) WHERE 1",
        "CREATE INDEX ix_run_inputs_locator "
        "ON run_inputs(run_id COLLATE NOCASE) WHERE 1",
        "CREATE INDEX ix_run_inputs_locator "
        "ON run_inputs(run_id COLLATE BINARY) WHERE 1",
        "CREATE INDEX ix_run_inputs_locator ON run(id COLLATE BINARY) WHERE 1",
    ],
)
async def test_run_input_layout_refuses_altered_main_indexes(
    tmp_path: Path, replacement: str
) -> None:
    database = await open_sqlite(tmp_path)
    try:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(DROP_INPUT_INDEX)
            await writer.exec_driver_sql(replacement)

        async with database.read_snapshot() as reader:
            with executed_queries(reader) as queries:
                observed = await observe_sqlite_run_input_layout(reader)

        assert observed is False
        assert len(queries) == 3
        assert database.units_in_flight() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "last_check"),
    [
        (
            "CREATE TABLE main.run_inputs "
            "(run_id VARCHAR(26) NOT NULL PRIMARY KEY, "
            "cel_projection BLOB NOT NULL, params TEXT NOT NULL)",
            TABLE_XINFO[-1],
        ),
        (
            "CREATE TABLE main.run_inputs " + INPUT_TABLE_BODY + " WITHOUT ROWID",
            INDEX_XINFO[-1],
        ),
    ],
    ids=["wrong-type", "without-rowid"],
)
async def test_run_input_layout_refuses_altered_physical_table(
    tmp_path: Path, definition: str, last_check: str
) -> None:
    database = await open_sqlite(tmp_path)
    try:
        async with database.transaction() as writer:
            index = await input_index_definition(writer)
            await writer.exec_driver_sql("DROP TABLE main.run_inputs")
            await writer.exec_driver_sql(definition)
            await writer.exec_driver_sql(index)

        async with database.read_snapshot() as reader:
            with executed_queries(reader) as queries:
                observed = await observe_sqlite_run_input_layout(reader)

        assert observed is False
        assert queries[-1][0] == last_check
        assert database.units_in_flight() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", ["temp", "alternate"])
@pytest.mark.parametrize("main_present", [True, False], ids=["main", "no-main"])
async def test_run_input_layout_binds_main_despite_namesakes(
    tmp_path: Path, schema: str, main_present: bool
) -> None:
    database = await open_sqlite(tmp_path)
    try:
        async with database.transaction() as writer:
            definition = await input_index_definition(writer)
            if not main_present:
                await writer.exec_driver_sql(DROP_INPUT_INDEX)
        async with database.reader_engine.connect() as setup:
            await setup.exec_driver_sql("PRAGMA query_only = 0")
            if schema == "alternate":
                await setup.exec_driver_sql("ATTACH DATABASE ':memory:' AS alternate")
            if main_present:
                await setup.exec_driver_sql(f"CREATE TABLE {schema}.run_inputs (wrong)")
                await setup.exec_driver_sql(
                    f"CREATE INDEX {schema}.ix_run_inputs_locator ON run_inputs(wrong)"
                )
            else:
                await setup.exec_driver_sql(
                    f"CREATE TABLE {schema}.run_inputs " + INPUT_TABLE_BODY
                )
                await setup.exec_driver_sql(
                    definition.replace("CREATE INDEX ", f"CREATE INDEX {schema}.", 1)
                )
            await setup.exec_driver_sql("PRAGMA query_only = 1")
            await setup.commit()
            original = await writer_driver(setup)

        async with database.read_snapshot() as reader:
            observed_driver = await writer_driver(reader)
            observed = await observe_sqlite_run_input_layout(reader)

        assert observed_driver is original
        assert observed is main_present
        assert database.units_in_flight() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_run_input_layout_native_denial_is_private_and_releases(
    tmp_path: Path,
) -> None:
    database = await open_sqlite(tmp_path)
    ledger = ConnectionLedger(database)
    denied: list[str] = []

    def deny_index_xinfo(
        action: int,
        name: str | None,
        argument: str | None,
        schema: str | None,
        origin: str | None,
    ) -> int:
        if (
            action == sqlite3.SQLITE_PRAGMA
            and name == "index_xinfo"
            and argument == INPUT_INDEX
        ):
            denied.append(argument)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    try:
        clock = FakeClock()
        settings = LoggingSettings(
            level=Level.DEBUG,
            component_levels={DB_COMPONENT: Level.DEBUG},
            third_party={"sqlalchemy": Level.DEBUG, "aiosqlite": Level.DEBUG},
        )
        with captured(DB_COMPONENT, settings, "catalog", clock.now) as capture:
            async with database.read_snapshot() as reader:
                driver = await writer_driver(reader)
                await driver.set_authorizer(deny_index_xinfo)
                try:
                    refused = await observe_sqlite_run_input_layout(reader)
                finally:
                    await driver.set_authorizer(None)
            async with database.read_snapshot() as reader:
                recovered = await observe_sqlite_run_input_layout(reader)
            records = capture.finish()
            diagnostics = capture.reader.data

        assert refused is False
        assert recovered is True
        assert denied == [INPUT_INDEX]
        assert any(record["event"] == "thirdparty.record" for record in records)
        assert all(
            value.encode() not in diagnostics
            for value in (INPUT_INDEX, "not authorized", "Traceback", "PRAGMA")
        )
        assert ledger.outstanding == database.units_in_flight() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_run_input_layout_refuses_native_transaction_lost_after_xinfo(
    tmp_path: Path,
) -> None:
    database = await open_sqlite(tmp_path)
    observed_native: list[bool] = []
    try:
        async with database.read_snapshot() as reader:
            driver = await writer_driver(reader)

            def end_native(
                connection: object, cursor: object, statement: str, *arguments: object
            ) -> None:
                if statement == INDEX_XINFO[-1]:
                    observed_native.append(driver.in_transaction)
                    await_(driver.rollback())

            event.listen(reader.sync_connection, "after_cursor_execute", end_native)
            try:
                observed = await observe_sqlite_run_input_layout(reader)
            finally:
                event.remove(reader.sync_connection, "after_cursor_execute", end_native)

        assert observed_native == [True]
        assert observed is False
        assert database.units_in_flight() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_run_input_layout_cancellation_keeps_entered_catalog_owner(
    tmp_path: Path,
) -> None:
    database = await open_sqlite(tmp_path)
    ledger = ConnectionLedger(database)
    entered = asyncio.Event()
    resume = asyncio.Event()
    cancellation_delivered = asyncio.Event()
    handles: list[SQLiteDriver] = []
    cursors: list[SQLiteCursor] = []

    def hold_xinfo(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement == INDEX_XINFO[-1]:
            native_cursor: object = getattr(cursor, "_cursor", None)
            assert isinstance(native_cursor, SQLiteCursor)
            cursors.append(native_cursor)
            entered.set()
            await_(resume.wait())

    async def observe() -> bool:
        async with database.read_snapshot() as reader:
            handles.append(await writer_driver(reader))
            event.listen(reader.sync_connection, "after_cursor_execute", hold_xinfo)
            try:
                return await observe_sqlite_run_input_layout(reader)
            finally:
                event.remove(reader.sync_connection, "after_cursor_execute", hold_xinfo)

    try:
        task = asyncio.create_task(observe())
        try:
            await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)
            task.cancel("catalog-cancelled")
            asyncio.get_running_loop().call_soon(cancellation_delivered.set)
            await cancellation_delivered.wait()
            held = (
                ledger.outstanding,
                database.units_in_flight(),
                handles[0].in_transaction,
            )
        finally:
            resume.set()
            with pytest.raises(
                asyncio.CancelledError, match="catalog-cancelled"
            ) as failure:
                await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
        ended = not handles[0].in_transaction
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            await cursors[0].fetchone()
        async with database.read_snapshot() as reader:
            recovered_driver = await writer_driver(reader)
            recovered = await observe_sqlite_run_input_layout(reader)

        assert held == (1, 1, True)
        assert failure.value.__context__ is failure.value.__cause__ is None
        assert ended
        assert recovered_driver is handles[0]
        assert recovered is True
        assert ledger.outstanding == database.units_in_flight() == 0
    finally:
        await database.close()
