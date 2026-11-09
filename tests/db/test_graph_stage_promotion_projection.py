from traceback import format_exception, walk_tb
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Dialect
from kora.db.graph_stage_promotion_custody_v1 import (
    INTENT_TABLE,
    MAX_PROMOTION_DOCUMENT_BYTES_V1,
    SETTLEMENT_TABLE,
)
from kora.db.graph_stage_promotion_projection import (
    MAX_PROMOTION_PAIR_BYTES,
    PromotionDocumentProjectionRejected,
    graph_stage_promotion_document_comparison_statement,
    graph_stage_promotion_document_statement,
)
from kora.db.tables import graph_deployment_stage_promotion_intent as intent
from kora.db.tables import graph_deployment_stage_promotion_settlement as settlement
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.db.test_graph_stage_promotion_custody_migration import (
    BOUNDARY_BYTES,
    INTENT_INSERT,
    SETTLEMENT_INSERT,
    seed_stage,
)

PRIVATE: str = "private-projection-input"
WIDTH: int = MAX_PROMOTION_DOCUMENT_BYTES_V1


async def projected(
    connection: AsyncConnection, stage_id: str, allowance: int
) -> tuple[object, ...]:
    statement = graph_stage_promotion_document_statement(
        connection.dialect.name, stage_id, max_document_bytes=allowance
    )
    result = await connection.execute(statement)
    try:
        return tuple(result.one())
    finally:
        result.close()


async def compared(
    connection: AsyncConnection,
    stage_id: str,
    request: bytes | None,
    observation: bytes | None,
) -> bool:
    statement = graph_stage_promotion_document_comparison_statement(
        connection.dialect.name,
        stage_id,
        request_bytes=request,
        observation_bytes=observation,
    )
    result = await connection.execute(statement)
    try:
        (matched,) = tuple(result.one())
    finally:
        result.close()
    assert type(matched) is bool
    return matched


@pytest.mark.asyncio
async def test_promotion_document_comparison_matches_exactly_without_returning_bytes(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    observation = BOUNDARY_BYTES[::-1]
    wrong_request = BOUNDARY_BYTES[:-1] + bytes((BOUNDARY_BYTES[-1] ^ 1,))
    wrong_observation = observation[:-1] + bytes((observation[-1] ^ 1,))
    async with database.transaction() as connection:
        selected = await seed_stage(connection, "comparison")
        pending = await seed_stage(connection, "comparison-unsettled")
        missing = Seeder(connection).new_id()
        await connection.execute(
            INTENT_INSERT,
            [
                selected.request("comparison", BOUNDARY_BYTES),
                pending.request("comparison-unsettled", BOUNDARY_BYTES),
            ],
        )
        await connection.execute(
            SETTLEMENT_INSERT,
            {"stage_id": selected.stage_id, "observation_bytes": observation},
        )
    alternatives = (
        (selected.stage_id, BOUNDARY_BYTES, None),
        (selected.stage_id, None, None),
        (pending.stage_id, BOUNDARY_BYTES, observation),
        (pending.stage_id, None, None),
        (missing, BOUNDARY_BYTES, None),
        (selected.stage_id, wrong_request, observation),
        (selected.stage_id, BOUNDARY_BYTES, wrong_observation),
    )

    async with database.read_snapshot() as connection:
        checkouts = ledger.checkouts
        with executed_queries(connection) as queries:
            exact = await compared(
                connection, selected.stage_id, BOUNDARY_BYTES, observation
            )
        intent_only = await compared(connection, pending.stage_id, BOUNDARY_BYTES, None)
        absent = await compared(connection, missing, None, None)
        alternatives_match = [
            await compared(connection, stage_id, request, receipt)
            for stage_id, request, receipt in alternatives
        ]
        assert ledger.checkouts == checkouts

    assert exact is intent_only is absent is True
    assert alternatives_match == [False] * len(alternatives)
    [(sql, parameters)] = queries
    assert sql.count("LEFT JOIN") == 2
    if database.dialect is Dialect.SQLITE:
        assert sql.count("typeof(") == 2
        assert sql.count("length(") == 2
        assert (
            "THEN intent.request_bytes = ? AND settlement.observation_bytes = ?" in sql
        )
        assert parameters == (
            "blob",
            WIDTH,
            "blob",
            WIDTH,
            BOUNDARY_BYTES,
            observation,
            selected.stage_id,
        )
    else:
        assert sql.count("pg_catalog.octet_length(") == 2
        assert sql.count("OPERATOR(pg_catalog.=)") == 6
        assert sql.count("AS pg_catalog.bytea)") == 2
        assert sql.count("AS pg_catalog.int8)") == 2
        assert "::BYTEA" not in sql.upper()
        assert parameters == (
            WIDTH,
            WIDTH,
            BOUNDARY_BYTES,
            observation,
            selected.stage_id,
        )
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_promotion_document_comparison_uses_native_postgresql_operators(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    async with database.transaction() as connection:
        selected = await seed_stage(connection, "comparison-native")
        await connection.execute(
            INTENT_INSERT, selected.request("comparison-native", b"request")
        )
        if database.dialect is Dialect.POSTGRESQL:
            await connection.exec_driver_sql("CREATE SCHEMA promotion_shadow")
            await connection.exec_driver_sql(
                "CREATE FUNCTION promotion_shadow.always_equal("
                "pg_catalog.int8, pg_catalog.int8) RETURNS pg_catalog.bool "
                "IMMUTABLE LANGUAGE SQL AS 'SELECT true'"
            )
            await connection.exec_driver_sql(
                "CREATE OPERATOR promotion_shadow.= ("
                "LEFTARG = pg_catalog.int8, RIGHTARG = pg_catalog.int8, "
                "FUNCTION = promotion_shadow.always_equal)"
            )
            await connection.exec_driver_sql(
                "CREATE DOMAIN promotion_shadow.bytea AS pg_catalog.text"
            )

    async with database.read_snapshot() as connection:
        if database.dialect is Dialect.POSTGRESQL:
            await connection.exec_driver_sql(
                "SET LOCAL search_path TO promotion_shadow, pg_catalog, public"
            )
            shadowed = await connection.exec_driver_sql(
                "SELECT 1::pg_catalog.int8 = 2::pg_catalog.int8, "
                "pg_catalog.pg_typeof(CAST(NULL AS bytea)) "
                "OPERATOR(pg_catalog.=) pg_catalog.to_regtype('promotion_shadow.bytea')"
            )
            try:
                assert tuple(shadowed.one()) == (True, True)
            finally:
                shadowed.close()
        correct = await compared(connection, selected.stage_id, b"request", None)
        wrong_width = await compared(connection, selected.stage_id, b"r", None)
        same_width = await compared(connection, selected.stage_id, b"replica", None)

    assert correct is True
    assert wrong_width is same_width is False
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_promotion_document_projection_admits_both_or_neither(
    database: Database,
) -> None:
    reversed_body = BOUNDARY_BYTES[::-1]
    async with database.transaction() as connection:
        selected = await seed_stage(connection, "selected")
        pending = await seed_stage(connection, "unsettled")
        missing = Seeder(connection).new_id()
        await connection.execute(
            INTENT_INSERT,
            [
                selected.request("selected", BOUNDARY_BYTES),
                pending.request("unsettled", BOUNDARY_BYTES),
            ],
        )
        await connection.execute(
            SETTLEMENT_INSERT,
            {"stage_id": selected.stage_id, "observation_bytes": reversed_body},
        )

    async with database.read_snapshot() as connection:
        pair = await projected(connection, selected.stage_id, MAX_PROMOTION_PAIR_BYTES)
        one_short = await projected(
            connection, selected.stage_id, MAX_PROMOTION_PAIR_BYTES - 1
        )
        zero = await projected(connection, selected.stage_id, 0)
        unsettled = await projected(connection, pending.stage_id, WIDTH)
        unsettled_short = await projected(connection, pending.stage_id, WIDTH - 1)
        absent = await projected(connection, missing, MAX_PROMOTION_PAIR_BYTES)

    assert pair == (True, True, WIDTH, WIDTH, BOUNDARY_BYTES, reversed_body)
    assert one_short == zero == (True, True, WIDTH, WIDTH, None, None)
    assert unsettled == (True, False, WIDTH, None, BOUNDARY_BYTES, None)
    assert unsettled_short == (True, False, WIDTH, None, None, None)
    assert absent == (False, False, None, None, None, None)
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_promotion_document_projection_refuses_damaged_witnesses(
    database: Database,
) -> None:
    wrong_storage: tuple[object, ...] | None = None
    damaged_matches: list[bool] = []
    wide = BOUNDARY_BYTES + b"x"
    async with database.transaction() as connection:
        selected = await seed_stage(connection, "damaged")
        await connection.execute(
            INTENT_INSERT, selected.request("damaged", BOUNDARY_BYTES)
        )
        await connection.execute(
            SETTLEMENT_INSERT,
            {"stage_id": selected.stage_id, "observation_bytes": BOUNDARY_BYTES},
        )

    async with database.transaction() as connection:
        savepoint = await connection.begin_nested()
        try:
            if database.dialect is Dialect.SQLITE:
                await connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
                await connection.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
            else:
                await connection.exec_driver_sql(
                    f"ALTER TABLE {INTENT_TABLE} DROP CONSTRAINT "
                    "ck_promotion_intent_document_width"
                )
                await connection.exec_driver_sql(
                    f"ALTER TABLE {SETTLEMENT_TABLE} DROP CONSTRAINT "
                    "ck_promotion_settlement_document_width, "
                    "DROP CONSTRAINT fk_promotion_settlement_intent"
                )
            settlement_update = settlement.update().where(
                settlement.c.stage_id == selected.stage_id
            )
            await connection.execute(settlement_update.values(observation_bytes=b""))
            empty = await projected(
                connection, selected.stage_id, MAX_PROMOTION_PAIR_BYTES
            )
            damaged_matches.append(
                await compared(
                    connection, selected.stage_id, BOUNDARY_BYTES, BOUNDARY_BYTES
                )
            )
            await connection.execute(settlement_update.values(observation_bytes=wide))
            oversized_settlement = await projected(
                connection, selected.stage_id, MAX_PROMOTION_PAIR_BYTES
            )
            damaged_matches.append(
                await compared(
                    connection, selected.stage_id, BOUNDARY_BYTES, BOUNDARY_BYTES
                )
            )
            if database.dialect is Dialect.SQLITE:
                await connection.exec_driver_sql(
                    "UPDATE graph_deployment_stage_promotion_settlement "
                    "SET observation_bytes = ? WHERE stage_id = ?",
                    (PRIVATE, selected.stage_id),
                )
                wrong_storage = await projected(
                    connection, selected.stage_id, MAX_PROMOTION_PAIR_BYTES
                )
                damaged_matches.append(
                    await compared(
                        connection, selected.stage_id, BOUNDARY_BYTES, BOUNDARY_BYTES
                    )
                )
            await connection.execute(
                settlement_update.values(observation_bytes=BOUNDARY_BYTES)
            )
            await connection.execute(
                intent.update()
                .where(intent.c.stage_id == selected.stage_id)
                .values(request_bytes=wide)
            )
            oversized_intent = await projected(
                connection, selected.stage_id, MAX_PROMOTION_PAIR_BYTES
            )
            damaged_matches.append(
                await compared(
                    connection, selected.stage_id, BOUNDARY_BYTES, BOUNDARY_BYTES
                )
            )
            await connection.execute(
                intent.delete().where(intent.c.stage_id == selected.stage_id)
            )
            orphan = await projected(
                connection, selected.stage_id, MAX_PROMOTION_PAIR_BYTES
            )
            damaged_matches.append(
                await compared(connection, selected.stage_id, None, None)
            )
        finally:
            await savepoint.rollback()
            if database.dialect is Dialect.SQLITE:
                await connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
                await connection.exec_driver_sql("PRAGMA defer_foreign_keys=OFF")
    async with database.read_snapshot() as connection:
        restored = await projected(
            connection, selected.stage_id, MAX_PROMOTION_PAIR_BYTES
        )

    assert empty == (True, True, WIDTH, 0, None, None)
    assert oversized_settlement == (True, True, WIDTH, WIDTH + 1, None, None)
    assert oversized_intent == (True, True, WIDTH + 1, WIDTH, None, None)
    if database.dialect is Dialect.SQLITE:
        assert wrong_storage == (True, True, WIDTH, None, None, None)
    assert orphan == (False, True, None, WIDTH, None, None)
    assert damaged_matches == [False] * (5 if database.dialect is Dialect.SQLITE else 4)
    assert restored == (True, True, WIDTH, WIDTH, BOUNDARY_BYTES, BOUNDARY_BYTES)
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_promotion_document_projection_uses_both_exact_native_keys(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        history = [
            await seed_stage(connection, f"history_{index}") for index in range(128)
        ]
        await connection.execute(
            INTENT_INSERT,
            [
                stage.request(f"operation_{index}", b"i" * 1024)
                for index, stage in enumerate(history)
            ],
        )
        await connection.execute(
            SETTLEMENT_INSERT,
            [
                {"stage_id": stage.stage_id, "observation_bytes": b"s" * 1024}
                for stage in history
            ],
        )
        await connection.exec_driver_sql(f"ANALYZE {INTENT_TABLE}")
        await connection.exec_driver_sql(f"ANALYZE {SETTLEMENT_TABLE}")
        missing = Seeder(connection).new_id()
    sqlite = database.dialect is Dialect.SQLITE
    prefix = "EXPLAIN QUERY PLAN " if sqlite else "EXPLAIN (COSTS OFF) "

    async with database.read_snapshot() as connection:
        plans: list[str] = []
        for key in (history[0].stage_id, missing):
            with executed_queries(connection) as queries:
                await projected(connection, key, MAX_PROMOTION_PAIR_BYTES)
            [(statement, arguments)] = queries
            result = await connection.exec_driver_sql(prefix + statement, arguments)
            try:
                plans.append("\n".join(str(tuple(row)) for row in result))
            finally:
                result.close()

    for plan in plans:
        for relation in (INTENT_TABLE, SETTLEMENT_TABLE):
            if sqlite:
                assert f"USING INDEX sqlite_autoindex_{relation}_1 (stage_id=?)" in plan
            else:
                assert f"Index Scan using pk_{relation}" in plan
        if not sqlite:
            assert plan.count("Index Cond: (stage_id = ") == 2


@pytest.mark.asyncio
async def test_promotion_document_projection_never_matches_a_cased_variant(
    database: Database,
) -> None:
    sqlite = database.dialect is Dialect.SQLITE
    async with database.transaction() as connection:
        selected = await seed_stage(connection, "selected")
        await connection.execute(
            INTENT_INSERT, selected.request("selected", BOUNDARY_BYTES)
        )
        if sqlite:
            await connection.exec_driver_sql(
                "DROP TABLE graph_deployment_stage_promotion_settlement"
            )
            await connection.exec_driver_sql(
                "CREATE TABLE graph_deployment_stage_promotion_settlement ("
                "stage_id TEXT COLLATE NOCASE NOT NULL, "
                "observation_bytes BLOB NOT NULL, "
                "PRIMARY KEY (stage_id COLLATE BINARY))"
            )
        else:
            await connection.exec_driver_sql(
                "ALTER TABLE graph_deployment_stage_promotion_settlement "
                "DROP CONSTRAINT ck_promotion_settlement_stage_id_domain, "
                "DROP CONSTRAINT fk_promotion_settlement_intent"
            )
        variant = selected.stage_id.lower()
        assert variant != selected.stage_id
        await connection.execute(
            SETTLEMENT_INSERT,
            {"stage_id": variant, "observation_bytes": b"foreign settlement"},
        )

    plan = ""
    async with database.read_snapshot() as connection:
        with executed_queries(connection) as queries:
            exact = await projected(
                connection, selected.stage_id, MAX_PROMOTION_PAIR_BYTES
            )
        if sqlite:
            [(statement, arguments)] = queries
            result = await connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN " + statement, arguments
            )
            try:
                plan = "\n".join(str(tuple(row)) for row in result)
            finally:
                result.close()

    assert exact == (True, False, WIDTH, None, BOUNDARY_BYTES, None)
    if sqlite:
        for relation in (INTENT_TABLE, SETTLEMENT_TABLE):
            assert f"USING INDEX sqlite_autoindex_{relation}_1 (stage_id=?)" in plan


@pytest.mark.parametrize(
    ("dialect", "stage_id", "allowance"),
    [
        (PRIVATE, "0" * 26, 0),
        ("sqlite", PRIVATE, 0),
        ("postgresql", "0" * 26, True),
        ("sqlite", "0" * 26, -1),
        ("postgresql", "0" * 26, MAX_PROMOTION_PAIR_BYTES + 1),
    ],
)
def test_promotion_document_projection_refuses_invalid_arguments_privately(
    dialect: str, stage_id: str, allowance: int
) -> None:
    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        with pytest.raises(PromotionDocumentProjectionRejected) as refused:
            graph_stage_promotion_document_statement(
                dialect, stage_id, max_document_bytes=allowance
            )

    error = refused.value
    wrapper = next(
        frame.f_locals
        for frame, _ in walk_tb(error.__traceback__)
        if frame.f_code.co_name == "graph_stage_promotion_document_statement"
    )
    assert wrapper == {}
    assert error.__context__ is error.__cause__ is None
    assert PRIVATE not in "".join(format_exception(error))


@pytest.mark.parametrize(
    ("dialect", "stage_id", "expected_request", "expected_observation"),
    [
        (PRIVATE, "0" * 26, None, None),
        ("sqlite", PRIVATE, None, None),
        ("postgresql", "0" * 26, cast("bytes", bytearray(PRIVATE.encode())), None),
        ("sqlite", "0" * 26, b"", None),
        ("postgresql", "0" * 26, b"request", b""),
        ("sqlite", "0" * 26, BOUNDARY_BYTES + b"x", None),
        ("postgresql", "0" * 26, b"request", BOUNDARY_BYTES + b"x"),
        ("postgresql", "0" * 26, None, PRIVATE.encode()),
    ],
)
def test_promotion_document_comparison_refuses_invalid_expected_values_privately(
    dialect: str,
    stage_id: str,
    expected_request: bytes | None,
    expected_observation: bytes | None,
) -> None:
    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        with pytest.raises(PromotionDocumentProjectionRejected) as refused:
            graph_stage_promotion_document_comparison_statement(
                dialect,
                stage_id,
                request_bytes=expected_request,
                observation_bytes=expected_observation,
            )

    error = refused.value
    wrapper = next(
        frame.f_locals
        for frame, _ in walk_tb(error.__traceback__)
        if frame.f_code.co_name == "graph_stage_promotion_document_comparison_statement"
    )
    assert wrapper == {}
    assert error.__context__ is error.__cause__ is None
    assert PRIVATE not in "".join(format_exception(error))
