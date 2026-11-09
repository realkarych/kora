import pytest

from kora.db.connection import Database, Dialect
from kora.db.graph_stage_promotion_layout import (
    POSTGRES_LAYOUT,
    SQLITE_COLUMNS,
    SQLITE_KEYS,
    SQLITE_QUERY_ONLY,
    SQLITE_RELATIONS,
    GraphStagePromotionLayout,
    observe_graph_stage_promotion_layout,
)
from kora.db.graph_stage_promotion_projection import (
    MAX_PROMOTION_PAIR_BYTES,
    graph_stage_promotion_document_statement,
)
from tests.db.closing import ConnectionLedger
from tests.db.test_graph_read_admission import executed_queries
from tests.db.test_graph_stage_promotion_custody_migration import (
    INTENT_INSERT,
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


@pytest.mark.asyncio
async def test_promotion_layout_observes_only_native_metadata(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    expected = (
        (SQLITE_QUERY_ONLY, SQLITE_RELATIONS, SQLITE_COLUMNS, SQLITE_KEYS)
        if database.dialect is Dialect.SQLITE
        else (POSTGRES_LAYOUT,)
    )

    async with database.read_snapshot() as connection:
        with executed_queries(connection) as observed:
            result = await observe_graph_stage_promotion_layout(connection)
        async with connection.begin_nested():
            with executed_queries(connection) as nested_queries:
                nested = await observe_graph_stage_promotion_layout(connection)
    stale = await observe_graph_stage_promotion_layout(connection)

    assert result is GraphStagePromotionLayout.VERIFIED
    assert [statement.strip() for statement, _ in observed] == [
        statement.text.strip() for statement in expected
    ]
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

    assert result is GraphStagePromotionLayout.REFUSED
    assert 1 <= len(observed) <= 4
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

    assert result is GraphStagePromotionLayout.REFUSED
    if sqlite:
        assert [statement.strip() for statement, _ in observed] == [
            SQLITE_QUERY_ONLY.text.strip(),
            SQLITE_RELATIONS.text.strip(),
        ]
    else:
        [(statement, _)] = observed
        assert statement.strip() == POSTGRES_LAYOUT.text.strip()
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
