import asyncio

import pytest
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from kora.conformance.capture import captured
from kora.db import DB_COMPONENT
from kora.db.connection import Database, Dialect
from kora.db.postgres_graph_publication_layout import (
    POSTGRES_GRAPH_PUBLICATION_LAYOUT,
    PostgresGraphPublicationLayout,
    check_postgres_graph_publication_layout,
)
from kora.db.transactions import managed_writer
from kora.observability import Level, LoggingSettings
from tests.db.closing import ConnectionLedger
from tests.db.seed import FakeClock
from tests.db.test_graph_read_admission import executed_queries

RELATIONS = (
    "graph",
    "graph_version",
    "graph_version_read_admission",
    "graph_version_resource_owner",
    "graph_deployment_preparation",
    "idempotency_record",
    "event",
)
PRIVATE = "publication-layout-private-canary"


class DeliberatePublicationLayoutRollback(RuntimeError):
    pass


async def refuse_without_query(connection: AsyncConnection) -> None:
    with executed_queries(connection) as queries:
        outcome = await check_postgres_graph_publication_layout(connection)

    assert outcome is PostgresGraphPublicationLayout.REFUSED
    assert queries == []


async def assert_original_layout(database: Database) -> None:
    async with database.transaction() as writer:
        outcome = await check_postgres_graph_publication_layout(writer)

    assert outcome is PostgresGraphPublicationLayout.VERIFIED


async def refuse_after_changes(database: Database, changes: tuple[str, ...]) -> None:
    async with database.transaction() as writer:
        for change in changes:
            await writer.exec_driver_sql(change)
        outcome = await check_postgres_graph_publication_layout(writer)
        assert outcome is PostgresGraphPublicationLayout.REFUSED
        raise DeliberatePublicationLayoutRollback()


@pytest.mark.asyncio
async def test_graph_publication_layout_checks_one_cold_managed_writer(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)

    async with database.transaction() as writer:
        with executed_queries(writer) as queries:
            outcome = await check_postgres_graph_publication_layout(writer)
        assert ledger.outstanding == ledger.checkouts == 1

    if database.dialect is Dialect.POSTGRESQL:
        assert outcome is PostgresGraphPublicationLayout.VERIFIED
        assert queries == [(POSTGRES_GRAPH_PUBLICATION_LAYOUT.text, ())]
    else:
        assert outcome is PostgresGraphPublicationLayout.REFUSED
        assert queries == []
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_graph_publication_layout_refuses_read_and_unmanaged_owners(
    database: Database,
) -> None:
    async with database.read_transaction() as reader:
        await refuse_without_query(reader)
    async with database.writer_engine.connect() as connection, connection.begin():
        await refuse_without_query(connection)

    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", RELATIONS)
async def test_graph_publication_layout_refuses_each_temporary_shadow(
    database: Database, relation: str
) -> None:
    if database.dialect is Dialect.SQLITE:
        async with database.transaction() as writer:
            await refuse_without_query(writer)
        return
    ledger = ConnectionLedger(database)
    changes = (f"CREATE TEMP TABLE {relation} (shadow int)",)

    with pytest.raises(DeliberatePublicationLayoutRollback):
        await refuse_after_changes(database, changes)
    await assert_original_layout(database)

    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        ("ALTER TABLE idempotency_record SET UNLOGGED",),
        ("ALTER TABLE idempotency_record RENAME TO missing_receipt_layout",),
        ("CREATE TABLE receipt_child () INHERITS (idempotency_record)",),
        (
            "CREATE TABLE receipt_parent ()",
            "ALTER TABLE idempotency_record INHERIT receipt_parent",
        ),
        ("SET TRANSACTION READ ONLY",),
        ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",),
    ],
    ids=("unlogged", "missing", "child", "parent", "read-only", "repeatable-read"),
)
async def test_graph_publication_layout_refuses_native_drift(
    database: Database, changes: tuple[str, ...]
) -> None:
    if database.dialect is Dialect.SQLITE:
        async with database.transaction() as writer:
            await refuse_without_query(writer)
        return
    ledger = ConnectionLedger(database)

    with pytest.raises(DeliberatePublicationLayoutRollback):
        await refuse_after_changes(database, changes)
    await assert_original_layout(database)

    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_graph_publication_layout_refuses_logical_autocommit(
    database: Database,
) -> None:
    if database.dialect is Dialect.SQLITE:
        async with database.transaction() as writer:
            await refuse_without_query(writer)
        return
    ledger = ConnectionLedger(database)

    async with database.writer_engine.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        async with connection.begin():
            with managed_writer(connection):
                await refuse_without_query(connection)
    await assert_original_layout(database)

    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_graph_publication_layout_refuses_native_autocommit_after_catalog(
    database: Database,
) -> None:
    if database.dialect is Dialect.SQLITE:
        async with database.transaction() as writer:
            await refuse_without_query(writer)
        return
    engine = create_async_engine(database.settings.url, isolation_level="AUTOCOMMIT")

    try:
        async with engine.connect() as connection, connection.begin():
            assert await connection.scalar(POSTGRES_GRAPH_PUBLICATION_LAYOUT) is True
            with managed_writer(connection), executed_queries(connection) as queries:
                outcome = await check_postgres_graph_publication_layout(connection)
    finally:
        await engine.dispose()

    assert queries == [(POSTGRES_GRAPH_PUBLICATION_LAYOUT.text, ())]
    assert outcome is PostgresGraphPublicationLayout.REFUSED


@pytest.mark.asyncio
async def test_graph_publication_layout_refusal_is_private_and_writer_reusable(
    database: Database,
) -> None:
    if database.dialect is Dialect.SQLITE:
        async with database.transaction() as writer:
            await refuse_without_query(writer)
        return
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(
        level=Level.DEBUG,
        component_levels={DB_COMPONENT: Level.DEBUG},
        third_party={"sqlalchemy": Level.DEBUG},
    )

    async def refuse_aborted_transaction() -> None:
        async with database.transaction() as writer:
            with pytest.raises(DBAPIError, match=PRIVATE):
                await writer.exec_driver_sql(
                    "DO $$ BEGIN RAISE EXCEPTION "
                    "'publication-layout-private-canary'; END $$"
                )
            outcome = await check_postgres_graph_publication_layout(writer)
            assert outcome is PostgresGraphPublicationLayout.REFUSED
            raise DeliberatePublicationLayoutRollback()

    with captured(
        DB_COMPONENT, settings, "graph-publication-layout-test", FakeClock().now
    ) as capture:
        with pytest.raises(DeliberatePublicationLayoutRollback) as rejected:
            await refuse_aborted_transaction()
        await assert_original_layout(database)
        records = capture.finish()
        diagnostics = capture.reader.data

    assert rejected.value.__cause__ is rejected.value.__context__ is None
    assert sum(record["event"] == "db.transaction.failed" for record in records) == 1
    assert any(
        record["event"] == "thirdparty.record"
        and str(record["logger"]).startswith("sqlalchemy")
        for record in records
    )
    assert PRIVATE.encode("utf-8") not in diagnostics
    assert b"InFailedSQLTransaction" not in diagnostics
    assert b"pg_catalog.to_regclass" not in diagnostics
    assert ledger.outstanding == database.units_in_flight() == 0
    assert ledger.opened == 1


@pytest.mark.asyncio
async def test_graph_publication_layout_cancel_releases_the_managed_writer(
    database: Database,
) -> None:
    if database.dialect is Dialect.SQLITE:
        async with database.transaction() as writer:
            await refuse_without_query(writer)
        return
    ledger = ConnectionLedger(database)
    engine = database.writer_engine.sync_engine
    admissions: list[int] = []

    def cancel_at_dispatch(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement == POSTGRES_GRAPH_PUBLICATION_LAYOUT.text:
            owner = asyncio.current_task()
            assert owner is not None
            admissions.append(ledger.outstanding)
            asyncio.get_running_loop().call_soon(owner.cancel)

    async def check_after_native_begin() -> None:
        async with database.transaction() as writer:
            await writer.exec_driver_sql("SELECT 1")
            await check_postgres_graph_publication_layout(writer)

    event.listen(engine, "before_cursor_execute", cancel_at_dispatch)
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(check_after_native_begin(), timeout=10)
    finally:
        event.remove(engine, "before_cursor_execute", cancel_at_dispatch)
    await assert_original_layout(database)

    assert admissions == [1]
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
