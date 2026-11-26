from collections.abc import Mapping

import pytest
from sqlalchemy import Executable, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Dialect
from kora.db.graph_stage_promotion_custody_v1 import INTENT_TABLE, SETTLEMENT_TABLE
from kora.db.graph_stage_promotion_intent_reads import (
    POSTGRES_METADATA,
    POSTGRES_SCALARS,
    SQLITE_SCALARS,
)
from kora.db.graph_stage_promotion_projection import (
    MAX_PROMOTION_PAIR_BYTES,
    graph_stage_promotion_document_statement,
)
from tests.db.closing import ConnectionLedger
from tests.db.test_graph_read_admission import executed_queries
from tests.db.test_graph_stage_promotion_custody_migration import (
    INTENT_INSERT,
    SETTLEMENT_INSERT,
    OriginalStage,
    seed_stage,
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
async def test_promotion_projection_uses_the_exact_stage_key(
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
async def test_promotion_data_queries_exclude_alternate_relations(
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
        if sqlite:
            await writer.exec_driver_sql(f"DROP TABLE temp.{SETTLEMENT_TABLE}")
            await writer.exec_driver_sql(f"DROP TABLE temp.{INTENT_TABLE}")

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
