import asyncio
import sqlite3
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from kora.db.connection import Database, Dialect
from kora.db.migrations import upgrade_database
from kora.db.sqlite_graph_version_layout import (
    COLUMN_LAYOUT,
    ENCODING,
    PAGE_SIZE,
    TABLE_LAYOUT,
    XINFO_SHADOW,
    SQLiteGraphVersionLayout,
    read_sqlite_graph_version_layout,
)
from tests.db.closing import CONDITION_DEADLINE_SECONDS, ConnectionLedger
from tests.db.test_graph_read_admission import executed_queries
from tests.db.test_graph_value_migration import encoding_predecessor
from tests.db.test_sqlite_writer_entry import writer_driver
from tests.support.databases import encoded_sqlite_settings

PROBE_STATEMENTS = (TABLE_LAYOUT, XINFO_SHADOW, COLUMN_LAYOUT, PAGE_SIZE, ENCODING)
CREATE_CANARY = text("CREATE TABLE layout_canary (value INTEGER)")
INSERT_CANARY = text("INSERT INTO layout_canary VALUES (7)")
READ_CANARY = text("SELECT value FROM layout_canary")
GRAPH_TABLE_DDL = text(
    "SELECT sql FROM main.sqlite_schema WHERE type = 'table' AND name = 'graph_version'"
)


class LayoutAbort(RuntimeError):
    pass


async def refuse_without_query(connection: AsyncConnection) -> None:
    with executed_queries(connection) as observed:
        layout = await read_sqlite_graph_version_layout(connection)

    assert layout is None
    assert observed == []


@pytest.mark.asyncio
async def test_sqlite_graph_layout_requires_the_current_managed_writer(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    page_size: object = None

    async with database.read_transaction() as reader:
        await refuse_without_query(reader)
    async with database.transaction() as writer:
        async with writer.begin_nested():
            await refuse_without_query(writer)
        await asyncio.create_task(refuse_without_query(writer))
        with executed_queries(writer) as queries:
            layout = await read_sqlite_graph_version_layout(writer)
        if database.dialect is Dialect.SQLITE:
            page_size = await writer.scalar(PAGE_SIZE)
    await refuse_without_query(writer)

    if database.dialect is Dialect.SQLITE:
        assert type(page_size) is int
        assert layout == SQLiteGraphVersionLayout(page_size, "utf-8")
        assert queries == [(statement.text, ()) for statement in PROBE_STATEMENTS]
    else:
        assert layout is None
        assert queries == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoding", "page_size", "populated"),
    [("UTF-16le", 512, True), ("UTF-16be", 65_536, False)],
)
async def test_sqlite_graph_layout_observes_native_page_geometry_after_migration(
    tmp_path: Path, encoding: str, page_size: int, populated: bool
) -> None:
    settings = encoded_sqlite_settings(
        tmp_path / "graph-layout.db", encoding, page_size
    )
    database = Database(settings)
    ledger = ConnectionLedger(database)

    try:
        if populated:
            await encoding_predecessor(database)
        migrated = await upgrade_database(settings)
        async with database.transaction() as writer:
            layout = await read_sqlite_graph_version_layout(writer)
    finally:
        await database.close()

    assert migrated.from_revision == ("0006" if populated else None)
    assert layout == SQLiteGraphVersionLayout(page_size, encoding.lower())
    assert ledger.outstanding == database.units_in_flight() == 0
    assert ledger.opened == ledger.closed > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "statements"),
    [
        ("missing-main", (TABLE_LAYOUT,)),
        ("rowid-free", (TABLE_LAYOUT,)),
        ("column-name", (TABLE_LAYOUT, XINFO_SHADOW, COLUMN_LAYOUT)),
        ("catalog-collision", (TABLE_LAYOUT, XINFO_SHADOW)),
    ],
)
async def test_sqlite_graph_layout_refuses_incompatible_main_catalog(
    database: Database,
    change: Literal["missing-main", "rowid-free", "column-name", "catalog-collision"],
    statements: tuple[TextClause, ...],
) -> None:
    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await refuse_without_query(writer)
        return
    ledger = ConnectionLedger(database)

    async def refuse_changed_layout() -> None:
        async with database.transaction() as writer:
            if change == "column-name":
                await writer.exec_driver_sql(
                    "ALTER TABLE main.graph_version RENAME COLUMN resources TO other"
                )
            elif change == "catalog-collision":
                await writer.exec_driver_sql(
                    "CREATE VIEW main.Pragma_Table_Xinfo AS SELECT 1"
                )
            else:
                definition: object = await writer.scalar(GRAPH_TABLE_DDL)
                assert type(definition) is str
                assert definition.startswith("CREATE TABLE graph_version (")
                await writer.exec_driver_sql(
                    "ALTER TABLE main.graph_version RENAME TO previous_graph_version"
                )
                if change == "rowid-free":
                    await writer.exec_driver_sql(definition + " WITHOUT ROWID")
                else:
                    await writer.exec_driver_sql(
                        definition.replace("CREATE TABLE", "CREATE TEMP TABLE", 1)
                    )
            with executed_queries(writer) as observed:
                layout = await read_sqlite_graph_version_layout(writer)

            assert layout is None
            assert observed == [(statement.text, ()) for statement in statements]
            raise LayoutAbort()

    with pytest.raises(LayoutAbort):
        await refuse_changed_layout()

    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_sqlite_graph_layout_ignores_temporary_namesakes(
    database: Database,
) -> None:
    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await refuse_without_query(writer)
        return

    async with database.transaction() as writer:
        original = await read_sqlite_graph_version_layout(writer)
        await writer.exec_driver_sql("CREATE TEMP TABLE graph_version (shadow TEXT)")
        await writer.exec_driver_sql("CREATE TEMP TABLE pragma_table_xinfo (n INTEGER)")

        layout = await read_sqlite_graph_version_layout(writer)
        await writer.exec_driver_sql("DROP TABLE temp.pragma_table_xinfo")
        await writer.exec_driver_sql("DROP TABLE temp.graph_version")

    assert original is not None
    assert layout == original


@pytest.mark.asyncio
async def test_sqlite_graph_layout_native_refusal_rolls_back_on_caller_abort(
    database: Database,
) -> None:
    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await refuse_without_query(writer)
        return
    ledger = ConnectionLedger(database)
    denied: list[bool] = []

    def deny_xinfo(
        action: int,
        pragma: str | None,
        argument: str | None,
        schema: str | None,
        origin: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_PRAGMA and pragma == "table_xinfo":
            denied.append(True)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    async with database.transaction() as writer:
        await writer.execute(CREATE_CANARY)

    async def refuse_native_observation() -> None:
        async with database.transaction() as writer:
            await writer.execute(INSERT_CANARY)
            driver = await writer_driver(writer)
            await driver.set_authorizer(deny_xinfo)
            try:
                layout = await read_sqlite_graph_version_layout(writer)
            finally:
                await driver.set_authorizer(None)
            assert layout is None
            raise LayoutAbort()

    with pytest.raises(LayoutAbort) as refused:
        await refuse_native_observation()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
    async with database.transaction() as writer:
        canary = (await writer.execute(READ_CANARY)).all()
        recovered = await read_sqlite_graph_version_layout(writer)

    assert denied == [True]
    assert refused.value.__context__ is None
    assert not canary
    assert recovered is not None
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_sqlite_graph_layout_cancellation_releases_writer(
    database: Database,
) -> None:
    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await refuse_without_query(writer)
        return
    ledger = ConnectionLedger(database)
    engine = database.writer_engine.sync_engine
    admissions: list[int] = []

    def cancel_at_dispatch(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement == COLUMN_LAYOUT.text:
            owner = asyncio.current_task()
            assert owner is not None
            admissions.append(ledger.outstanding)
            asyncio.get_running_loop().call_soon(owner.cancel)

    async def observe() -> None:
        async with database.transaction() as writer:
            await writer.execute(INSERT_CANARY)
            await read_sqlite_graph_version_layout(writer)

    async with database.transaction() as writer:
        await writer.execute(CREATE_CANARY)
    event.listen(engine, "before_cursor_execute", cancel_at_dispatch)
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(observe(), timeout=CONDITION_DEADLINE_SECONDS)
    finally:
        event.remove(engine, "before_cursor_execute", cancel_at_dispatch)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
    async with database.transaction() as writer:
        canary = (await writer.execute(READ_CANARY)).all()
        layout = await read_sqlite_graph_version_layout(writer)

    assert admissions == [1]
    assert not canary
    assert layout is not None
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
