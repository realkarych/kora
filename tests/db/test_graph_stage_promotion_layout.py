import asyncio
import sqlite3
import threading
from collections.abc import Mapping

import pytest
from aiosqlite import Connection as SQLiteDriver
from sqlalchemy import Executable, event, text
from sqlalchemy.engine import Dialect as SQLAlchemyDialect
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.conformance.capture import captured
from kora.db import DB_COMPONENT
from kora.db.connection import Database, Dialect
from kora.db.graph_stage_promotion_custody_v1 import INTENT_TABLE, SETTLEMENT_TABLE
from kora.db.graph_stage_promotion_intent_reads import (
    POSTGRES_METADATA,
    POSTGRES_SCALARS,
    SQLITE_SCALARS,
)
from kora.db.graph_stage_promotion_layout import (
    POSTGRES_LAYOUT,
    SQLITE_COLUMNS,
    SQLITE_KEYS,
    SQLITE_QUERY_ONLY,
    SQLITE_RELATIONS,
    GraphStagePromotionLayout,
    observe_graph_stage_promotion_layout,
    observe_graph_stage_promotion_writer_layout,
)
from kora.db.graph_stage_promotion_projection import (
    MAX_PROMOTION_PAIR_BYTES,
    graph_stage_promotion_document_statement,
)
from kora.db.transactions import writer_transaction
from kora.observability import Level, LoggingSettings
from tests.db.closing import CONDITION_DEADLINE_SECONDS, ConnectionLedger
from tests.db.seed import FakeClock
from tests.db.test_graph_read_admission import executed_queries
from tests.db.test_graph_stage_promotion_custody_migration import (
    INTENT_INSERT,
    SETTLEMENT_INSERT,
    OriginalStage,
    seed_stage,
)

DROP_SETTLEMENT: str = "DROP TABLE graph_deployment_stage_promotion_settlement"
VIEW_SETTLEMENT: str = (
    "CREATE VIEW graph_deployment_stage_promotion_settlement AS "
    "SELECT stage_id, request_bytes AS observation_bytes "
    "FROM graph_deployment_stage_promotion_intent"
)
SQLITE_WRONG_COLUMN_ORDER: str = (
    "CREATE TABLE graph_deployment_stage_promotion_settlement ("
    "observation_bytes BLOB NOT NULL, stage_id TEXT COLLATE BINARY NOT NULL, "
    "PRIMARY KEY (stage_id))"
)
POSTGRES_WRONG_COLUMN_ORDER: str = (
    "CREATE TABLE graph_deployment_stage_promotion_settlement ("
    'observation_bytes bytea NOT NULL, stage_id text COLLATE "C" NOT NULL, '
    "CONSTRAINT pk_graph_deployment_stage_promotion_settlement PRIMARY KEY (stage_id))"
)
SQLITE_WRONG_KEY: str = (
    "CREATE TABLE graph_deployment_stage_promotion_settlement ("
    "stage_id TEXT COLLATE BINARY NOT NULL, observation_bytes BLOB NOT NULL, "
    "PRIMARY KEY (stage_id COLLATE NOCASE))"
)
POSTGRES_WRONG_KEY: str = (
    "ALTER TABLE graph_deployment_stage_promotion_settlement "
    "DROP CONSTRAINT pk_graph_deployment_stage_promotion_settlement, "
    "ADD CONSTRAINT pk_graph_deployment_stage_promotion_settlement "
    "PRIMARY KEY (stage_id) DEFERRABLE INITIALLY IMMEDIATE"
)
SQLITE_NO_ROWID: str = (
    "CREATE TABLE graph_deployment_stage_promotion_settlement ("
    "stage_id TEXT COLLATE BINARY NOT NULL, observation_bytes BLOB NOT NULL, "
    "PRIMARY KEY (stage_id)) WITHOUT ROWID"
)
POSTGRES_RESTRICTED: str = (
    "ALTER TABLE graph_deployment_stage_promotion_settlement ENABLE ROW LEVEL SECURITY"
)
POSTGRES_SHADOW: str = (
    "CREATE TABLE promotion_shadow.graph_deployment_stage_promotion_settlement ("
    'stage_id text COLLATE "C" NOT NULL, observation_bytes bytea NOT NULL, '
    "CONSTRAINT pk_graph_deployment_stage_promotion_settlement PRIMARY KEY (stage_id))"
)
SQLITE_SPLIT_COLLATION_INTENT: str = (
    "CREATE TABLE graph_deployment_stage_promotion_intent ("
    "stage_id TEXT COLLATE NOCASE NOT NULL, store_id TEXT COLLATE BINARY NOT NULL, "
    "payload_digest TEXT COLLATE BINARY NOT NULL, fence_incarnation BIGINT NOT NULL, "
    "operation_id TEXT COLLATE BINARY NOT NULL, "
    "request_digest TEXT COLLATE BINARY NOT NULL, "
    "content_generation BIGINT NOT NULL, request_bytes BLOB NOT NULL, "
    "PRIMARY KEY (stage_id COLLATE BINARY))"
)
PRIVATE: str = "promotion-layout-private-canary"


class DeliberateLayoutRollback(RuntimeError):
    pass


def expected_catalog_queries(
    dialect: SQLAlchemyDialect, *, writable: bool
) -> list[tuple[str, tuple[object, ...]]]:
    if dialect.name == Dialect.SQLITE:
        return [
            (statement.text, ())
            for statement in (
                SQLITE_QUERY_ONLY,
                SQLITE_RELATIONS,
                SQLITE_COLUMNS,
                SQLITE_KEYS,
            )
        ]
    return [
        (
            str(POSTGRES_LAYOUT.compile(dialect=dialect)),
            ("read committed", "off") if writable else ("repeatable read", "on"),
        )
    ]


async def refuse_writer_without_query(connection: AsyncConnection) -> None:
    with executed_queries(connection) as observed:
        result = await observe_graph_stage_promotion_writer_layout(connection)

    assert result is GraphStagePromotionLayout.REFUSED
    assert observed == []


@pytest.mark.asyncio
async def test_promotion_layout_observes_only_native_metadata(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    async with database.read_snapshot() as connection:
        expected = expected_catalog_queries(connection.dialect, writable=False)
        with executed_queries(connection) as observed:
            result = await observe_graph_stage_promotion_layout(connection)
        async with connection.begin_nested():
            with executed_queries(connection) as nested_queries:
                nested = await observe_graph_stage_promotion_layout(connection)
    stale = await observe_graph_stage_promotion_layout(connection)

    assert result is GraphStagePromotionLayout.VERIFIED
    assert observed == expected
    assert nested is stale is GraphStagePromotionLayout.REFUSED
    assert nested_queries == []
    assert ledger.outstanding == 0
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_promotion_layout_refuses_an_ordinary_writer(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)

    async with database.transaction() as connection:
        with executed_queries(connection) as observed:
            result = await observe_graph_stage_promotion_layout(connection)

    assert result is GraphStagePromotionLayout.REFUSED
    assert len(observed) == 1
    assert ledger.outstanding == 0
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_promotion_writer_layout_observes_only_current_writer_catalog(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    async with database.transaction() as connection:
        lease = writer_transaction(connection)
        transaction = connection.get_transaction()
        checkouts = ledger.checkouts
        expected = expected_catalog_queries(connection.dialect, writable=True)

        with executed_queries(connection) as observed:
            result = await observe_graph_stage_promotion_writer_layout(connection)

        assert result is GraphStagePromotionLayout.VERIFIED
        assert observed == expected
        assert writer_transaction(connection) is lease
        assert connection.get_transaction() is transaction
        assert ledger.checkouts == checkouts
        assert ledger.outstanding == database.units_in_flight() == 1
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_promotion_writer_layout_refuses_inactive_or_foreign_lease(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)

    async with database.read_snapshot() as reader:
        await refuse_writer_without_query(reader)
    async with database.writer_engine.begin() as unmanaged:
        await refuse_writer_without_query(unmanaged)
    async with database.transaction() as writer:
        async with writer.begin_nested():
            await refuse_writer_without_query(writer)
        await asyncio.create_task(refuse_writer_without_query(writer))
        old_root = writer.get_transaction()
        await writer.rollback()
        async with writer.begin() as new_root:
            assert new_root is not old_root
            await refuse_writer_without_query(writer)
    await refuse_writer_without_query(writer)

    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_promotion_writer_layout_refuses_wrong_native_modes(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    sqlite = database.dialect is Dialect.SQLITE
    modes = (
        ("PRAGMA query_only=ON",)
        if sqlite
        else (
            "SET TRANSACTION READ ONLY",
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        )
    )
    results: list[GraphStagePromotionLayout] = []

    for mode in modes:
        async with database.transaction() as connection:
            await connection.exec_driver_sql(mode)
            checkouts = ledger.checkouts
            try:
                with executed_queries(connection) as observed:
                    results.append(
                        await observe_graph_stage_promotion_writer_layout(connection)
                    )
                assert (
                    observed
                    == expected_catalog_queries(connection.dialect, writable=True)[:1]
                )
                assert ledger.checkouts == checkouts
            finally:
                if sqlite:
                    await connection.exec_driver_sql("PRAGMA query_only=OFF")
            await connection.rollback()

    assert results == [GraphStagePromotionLayout.REFUSED] * len(modes)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


async def denied_sqlite_catalog_query(
    connection: AsyncConnection,
) -> GraphStagePromotionLayout:
    raw = await connection.get_raw_connection()
    driver: object = raw.driver_connection
    assert isinstance(driver, SQLiteDriver)
    denied = threading.Event()

    def authorize(
        action: int,
        target: str | None,
        value: str | None,
        database: str | None,
        source: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_PRAGMA and target == "query_only":
            denied.set()
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    await driver.set_authorizer(authorize)
    try:
        result = await observe_graph_stage_promotion_writer_layout(connection)
    finally:
        await driver.set_authorizer(None)
    assert denied.is_set()
    return result


@pytest.mark.asyncio
async def test_promotion_writer_layout_native_failure_is_private_and_releases_writer(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(
        level=Level.DEBUG,
        component_levels={DB_COMPONENT: Level.DEBUG},
        third_party={"sqlalchemy": Level.DEBUG, "aiosqlite": Level.DEBUG},
    )

    async def refuse_then_roll_back() -> None:
        async with database.transaction() as connection:
            checkouts = ledger.checkouts
            transaction = connection.get_transaction()
            if database.dialect is Dialect.SQLITE:
                result = await denied_sqlite_catalog_query(connection)
            else:
                with pytest.raises(DBAPIError, match=PRIVATE):
                    await connection.exec_driver_sql(
                        "DO $$ BEGIN RAISE EXCEPTION "
                        "'promotion-layout-private-canary'; END $$"
                    )
                result = await observe_graph_stage_promotion_writer_layout(connection)
            assert result is GraphStagePromotionLayout.REFUSED
            assert ledger.checkouts == checkouts
            assert connection.get_transaction() is transaction
            raise DeliberateLayoutRollback()

    with captured(
        DB_COMPONENT, settings, "promotion-layout-test", FakeClock().now
    ) as capture:
        with pytest.raises(DeliberateLayoutRollback) as rolled_back:
            await refuse_then_roll_back()
        assert ledger.outstanding == database.units_in_flight() == 0
        async with database.transaction() as connection:
            recovered = await observe_graph_stage_promotion_writer_layout(connection)
        records = capture.finish()
        diagnostics = capture.reader.data

    assert recovered is GraphStagePromotionLayout.VERIFIED
    assert rolled_back.value.__cause__ is rolled_back.value.__context__ is None
    assert sum(record["event"] == "db.transaction.failed" for record in records) == 1
    assert any(record["event"] == "thirdparty.record" for record in records)
    for hidden in (
        PRIVATE,
        "not authorized",
        "InFailedSQLTransaction",
        "pg_catalog.current_setting",
        "PRAGMA query_only",
        "RAISE EXCEPTION",
    ):
        assert hidden.encode() not in diagnostics
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_promotion_writer_layout_cancellation_releases_writer(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    engine = database.writer_engine.sync_engine
    expected = expected_catalog_queries(engine.dialect, writable=True)[0][0]
    admissions: list[int] = []

    def cancel_at_dispatch(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement != expected:
            return
        owner = asyncio.current_task()
        assert owner is not None
        admissions.append(ledger.outstanding)
        asyncio.get_running_loop().call_soon(owner.cancel)

    async def observe() -> None:
        async with database.transaction() as connection:
            await observe_graph_stage_promotion_writer_layout(connection)

    event.listen(engine, "before_cursor_execute", cancel_at_dispatch)
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(observe(), timeout=CONDITION_DEADLINE_SECONDS)
    finally:
        event.remove(engine, "before_cursor_execute", cancel_at_dispatch)
    async with database.transaction() as connection:
        recovered = await observe_graph_stage_promotion_writer_layout(connection)

    assert admissions == [1]
    assert recovered is GraphStagePromotionLayout.VERIFIED
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["missing", "view", "column_order", "key", "storage"]
)
async def test_promotion_layout_refuses_native_drift(
    database: Database, damage: str
) -> None:
    ledger = ConnectionLedger(database)
    sqlite = database.dialect is Dialect.SQLITE
    changes: tuple[str, ...]
    if damage == "missing":
        changes = (DROP_SETTLEMENT,)
    elif damage == "view":
        changes = (DROP_SETTLEMENT, VIEW_SETTLEMENT)
    elif damage == "column_order":
        changes = (
            DROP_SETTLEMENT,
            SQLITE_WRONG_COLUMN_ORDER if sqlite else POSTGRES_WRONG_COLUMN_ORDER,
        )
    elif damage == "key":
        changes = (
            (DROP_SETTLEMENT, SQLITE_WRONG_KEY) if sqlite else (POSTGRES_WRONG_KEY,)
        )
    else:
        changes = (
            (DROP_SETTLEMENT, SQLITE_NO_ROWID) if sqlite else (POSTGRES_RESTRICTED,)
        )
    async with database.transaction() as connection:
        for change in changes:
            await connection.exec_driver_sql(change)

    async with database.read_snapshot() as connection:
        with executed_queries(connection) as observed:
            result = await observe_graph_stage_promotion_layout(connection)
    async with database.transaction() as connection:
        with executed_queries(connection) as writer_queries:
            writer_result = await observe_graph_stage_promotion_writer_layout(
                connection
            )
        await connection.rollback()

    assert result is writer_result is GraphStagePromotionLayout.REFUSED
    assert 1 <= len(observed) <= 4
    assert (
        writer_queries
        == expected_catalog_queries(connection.dialect, writable=True)[: len(observed)]
    )
    assert ledger.outstanding == 0
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_promotion_layout_refuses_shadowed_catalog_or_source(
    database: Database,
) -> None:
    sqlite = database.dialect is Dialect.SQLITE
    async with database.transaction() as connection:
        if sqlite:
            await connection.exec_driver_sql(
                "CREATE TABLE main.Pragma_Index_XInfo (private_note TEXT)"
            )
        else:
            await connection.exec_driver_sql("CREATE SCHEMA promotion_shadow")
            await connection.exec_driver_sql(POSTGRES_SHADOW)

    async with database.read_snapshot() as connection:
        if not sqlite:
            await connection.exec_driver_sql(
                "SET LOCAL search_path TO promotion_shadow, pg_catalog, public"
            )
        with executed_queries(connection) as observed:
            result = await observe_graph_stage_promotion_layout(connection)
    async with database.transaction() as connection:
        if not sqlite:
            await connection.exec_driver_sql(
                "SET LOCAL search_path TO promotion_shadow, pg_catalog, public"
            )
        with executed_queries(connection) as writer_queries:
            writer_result = await observe_graph_stage_promotion_writer_layout(
                connection
            )
        await connection.rollback()

    assert result is writer_result is GraphStagePromotionLayout.REFUSED
    statements = 2 if sqlite else 1
    assert (
        observed
        == expected_catalog_queries(connection.dialect, writable=False)[:statements]
    )
    assert (
        writer_queries
        == expected_catalog_queries(connection.dialect, writable=True)[:statements]
    )
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_promotion_layout_and_projection_use_the_same_exact_stage_key(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    sqlite = database.dialect is Dialect.SQLITE
    async with database.transaction() as connection:
        stage = await seed_stage(connection, "selected")
        if sqlite:
            await connection.exec_driver_sql(
                "DROP TABLE graph_deployment_stage_promotion_intent"
            )
            await connection.exec_driver_sql(SQLITE_SPLIT_COLLATION_INTENT)
        else:
            await connection.exec_driver_sql(
                "ALTER TABLE graph_deployment_stage_promotion_intent "
                "DROP CONSTRAINT ck_promotion_intent_stage_id_domain, "
                "DROP CONSTRAINT fk_promotion_intent_stage"
            )
        variant = stage.stage_id.lower()
        assert variant != stage.stage_id
        await connection.execute(
            INTENT_INSERT,
            {**stage.request("other", b"other intent"), "stage_id": variant},
        )

    plan = ""
    async with database.read_snapshot() as connection:
        observation = await observe_graph_stage_promotion_layout(connection)
        assert observation is GraphStagePromotionLayout.VERIFIED
        statement = graph_stage_promotion_document_statement(
            connection.dialect.name,
            stage.stage_id,
            max_document_bytes=MAX_PROMOTION_PAIR_BYTES,
        )
        with executed_queries(connection) as projected:
            rows = await connection.execute(statement)
            try:
                result = tuple(rows.one())
            finally:
                rows.close()
        if sqlite:
            [(sql, arguments)] = projected
            explained = await connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN " + sql, arguments
            )
            try:
                plan = "\n".join(str(tuple(row)) for row in explained)
            finally:
                explained.close()

    assert result == (False, False, None, None, None, None)
    if sqlite:
        assert (
            "USING INDEX sqlite_autoindex_graph_deployment_stage_promotion_intent_1 "
            "(stage_id=?)"
        ) in plan
    assert ledger.outstanding == 0
    assert database.units_in_flight() == 0


async def _promotion_alternates(
    connection: AsyncConnection, stage: OriginalStage, *, sqlite: bool
) -> None:
    intent = INTENT_TABLE if sqlite else "alternate_promotion_intent"
    settlement = SETTLEMENT_TABLE if sqlite else "alternate_promotion_settlement"
    if sqlite:
        await connection.exec_driver_sql(
            "CREATE TEMP TABLE graph_deployment_stage_promotion_intent AS "
            "SELECT * FROM main.graph_deployment_stage_promotion_intent WHERE 0"
        )
        await connection.exec_driver_sql(
            "CREATE TEMP TABLE graph_deployment_stage_promotion_settlement AS "
            "SELECT * FROM main.graph_deployment_stage_promotion_settlement WHERE 0"
        )
    else:
        await connection.exec_driver_sql(
            f"CREATE TABLE {intent} (LIKE {INTENT_TABLE} INCLUDING CONSTRAINTS)"
        )
        await connection.exec_driver_sql(
            f"CREATE TABLE {settlement} (LIKE {SETTLEMENT_TABLE} INCLUDING CONSTRAINTS)"
        )
    await connection.execute(
        text(INTENT_INSERT.text.replace(INTENT_TABLE, intent, 1)),
        stage.request("alternate-operation", b"alternate intent"),
    )
    await connection.execute(
        text(SETTLEMENT_INSERT.text.replace(SETTLEMENT_TABLE, settlement, 1)),
        {"stage_id": stage.stage_id, "observation_bytes": b"alternate settlement"},
    )
    if not sqlite:
        for alternate, original in (
            (intent, INTENT_TABLE),
            (settlement, SETTLEMENT_TABLE),
        ):
            await connection.exec_driver_sql(
                f"ALTER TABLE {alternate} INHERIT {original}"
            )


async def _promotion_query_rows(
    connection: AsyncConnection,
    statement: Executable,
    arguments: Mapping[str, object] | None = None,
) -> tuple[tuple[object, ...], ...]:
    result = await connection.execute(statement, arguments)
    try:
        return tuple(tuple(row) for row in result.all())
    finally:
        result.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_state", ["settled", "intent", "empty"])
async def test_promotion_data_queries_exclude_alternate_relations_after_layout(
    database: Database, parent_state: str
) -> None:
    ledger = ConnectionLedger(database)
    sqlite = database.dialect is Dialect.SQLITE
    request = b"parent intent" if parent_state != "empty" else None
    settlement = b"parent settlement" if parent_state == "settled" else None
    operation = "parent-operation"
    async with database.transaction() as connection:
        stage = await seed_stage(connection, "parent")
        key = {"stage_id": stage.stage_id}
        if request is not None:
            await connection.execute(INTENT_INSERT, stage.request(operation, request))
        if settlement is not None:
            await connection.execute(
                SETTLEMENT_INSERT, {**key, "observation_bytes": settlement}
            )

    async with database.transaction() as writer:
        before = await observe_graph_stage_promotion_writer_layout(writer)
        if sqlite:
            await _promotion_alternates(writer, stage, sqlite=True)
        else:
            await writer.exec_driver_sql(
                f"LOCK TABLE ONLY {INTENT_TABLE}, ONLY {SETTLEMENT_TABLE} "
                "IN ACCESS SHARE MODE NOWAIT"
            )
            async with database.writer_engine.begin() as ddl:
                await ddl.exec_driver_sql("SET LOCAL lock_timeout = '2s'")
                await _promotion_alternates(ddl, stage, sqlite=False)
        scalars = await _promotion_query_rows(
            writer, SQLITE_SCALARS if sqlite else POSTGRES_SCALARS, key
        )
        widths = (
            ()
            if sqlite
            else await _promotion_query_rows(writer, POSTGRES_METADATA, key)
        )
        documents = await _promotion_query_rows(
            writer,
            graph_stage_promotion_document_statement(
                writer.dialect.name,
                stage.stage_id,
                max_document_bytes=MAX_PROMOTION_PAIR_BYTES,
            ),
        )
        intent_control = await writer.scalar(
            text(
                "SELECT count(*) FROM graph_deployment_stage_promotion_intent "
                "WHERE stage_id = :stage_id"
            ),
            key,
        )
        settlement_control = await writer.scalar(
            text(
                "SELECT count(*) FROM graph_deployment_stage_promotion_settlement "
                "WHERE stage_id = :stage_id"
            ),
            key,
        )
        after = await observe_graph_stage_promotion_writer_layout(writer)
        if sqlite:
            await writer.exec_driver_sql(f"DROP TABLE temp.{SETTLEMENT_TABLE}")
            await writer.exec_driver_sql(f"DROP TABLE temp.{INTENT_TABLE}")

    assert before is GraphStagePromotionLayout.VERIFIED
    assert after is (
        GraphStagePromotionLayout.VERIFIED
        if sqlite
        else GraphStagePromotionLayout.REFUSED
    )
    assert intent_control == 1 + int(not sqlite and request is not None)
    assert settlement_control == 1 + int(not sqlite and settlement is not None)
    expected = stage.request(operation, b"")
    assert scalars == (
        (
            (
                stage.store_id,
                stage.payload_digest,
                stage.fence_incarnation,
                operation,
                expected["request_digest"],
                expected["content_generation"],
            ),
        )
        if request is not None
        else ()
    )
    assert widths == (
        (
            (
                len(stage.store_id),
                len(stage.payload_digest),
                len(operation),
                len(str(expected["request_digest"])),
            ),
        )
        if not sqlite and request is not None
        else ()
    )
    assert documents == (
        (
            request is not None,
            settlement is not None,
            None if request is None else len(request),
            None if settlement is None else len(settlement),
            request,
            settlement,
        ),
    )
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
