from datetime import timedelta

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import DBAPIError

from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database, Dialect
from kora.db.tables import attempt
from kora.db.types import as_json_value
from kora.db.worker_claims import (
    claim_row,
    unknown_worker_slot,
    worker_slot_available,
)
from tests.db.seed import START, Seeder
from tests.db.test_grant_history_reads import plan_nodes
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.test_grant_history import seed


@pytest.mark.asyncio
async def test_worker_slots_count_live_open_and_block_unknown(
    database: Database,
) -> None:
    scope = await seed(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        second = await seeder.attempt(scope.node_id, number=2)
        third = await seeder.attempt(scope.node_id, number=3)
        await writer.execute(
            update(attempt).values(
                worker_id="worker-1",
                lease_until=START + timedelta(seconds=120),
                current_dispatcher_open=True,
            )
        )

    async with database.transaction() as writer:
        full = await worker_slot_available(writer, "worker-1", 2, START)
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == second)
            .values(
                current_dispatcher_open=False,
            )
        )
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == third)
            .values(
                lease_until=START,
            )
        )
        room = await worker_slot_available(writer, "worker-1", 2, START)
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == scope.attempt_id)
            .values(
                current_dispatcher_open=None,
            )
        )
        unknown = await unknown_worker_slot(writer, "worker-1", START)
        blocked = await worker_slot_available(writer, "worker-1", 2, START)
        expired = await worker_slot_available(
            writer, "worker-1", 2, START + timedelta(seconds=120)
        )

    assert (full, room, blocked, expired) == (False, True, False, True)
    assert unknown == scope.attempt_id
    async with database.read_transaction() as reader:
        assert (
            list(await reader.scalars(select(attempt.c.execution_state)))
            == ["unknown"] * 3
        )


@pytest.mark.asyncio
async def test_claim_scalar_read_refuses_wide_sqlite_row(database: Database) -> None:
    scope = await seed(database)
    async with database.transaction() as writer:
        await writer.execute(update(attempt).values(error={"content": "x" * 50_000}))

    async with database.read_transaction() as reader:
        if database.dialect is Dialect.SQLITE:
            with pytest.raises(ClaimSourcesRejected):
                await claim_row(
                    reader, attempt, attempt.c.id == scope.attempt_id, ("id",), 4096
                )
        else:
            row = await claim_row(
                reader, attempt, attempt.c.id == scope.attempt_id, ("id",), 4096
            )
            assert row is not None
            assert row["id"] == scope.attempt_id

    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False, True])
async def test_claim_boolean_projection_preserves_exact_state(
    database: Database,
    value: bool | None,
) -> None:
    scope = await seed(database)
    async with database.transaction() as writer:
        await writer.execute(update(attempt).values(current_dispatcher_open=value))

    async with database.read_transaction() as reader:
        saved = await claim_row(
            reader,
            attempt,
            attempt.c.id == scope.attempt_id,
            ("current_dispatcher_open",),
            65_536,
        )

    assert saved is not None
    assert saved["current_dispatcher_open"] is value


@pytest.mark.asyncio
async def test_claim_boolean_projection_refuses_malformed_state(
    database: Database,
) -> None:
    scope = await seed(database)

    if database.dialect is Dialect.SQLITE:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "UPDATE attempt SET current_dispatcher_open = 'invalid'"
            )
        async with database.read_transaction() as reader:
            with pytest.raises(ClaimSourcesRejected):
                await claim_row(
                    reader,
                    attempt,
                    attempt.c.id == scope.attempt_id,
                    ("current_dispatcher_open",),
                    65_536,
                )
    else:
        with pytest.raises(DBAPIError):
            async with database.transaction() as writer:
                await writer.exec_driver_sql(
                    "UPDATE attempt SET current_dispatcher_open = 'invalid'"
                )

    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_native_queries_seek_past_unrelated_and_expired_rows(
    database: Database,
) -> None:
    scope = await seed(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        template = dict((await writer.execute(select(attempt))).mappings().one())
        rows = [
            {
                **template,
                "id": seeder.new_id(),
                "number": number,
                "runtime": "foreign",
                "status": "succeeded",
                "worker_id": "worker-1",
                "lease_until": START,
                "current_dispatcher_open": True if number % 2 else None,
            }
            for number in range(2, 2050)
        ]
        await writer.execute(insert(attempt), rows)
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == scope.attempt_id)
            .values(
                worker_id="worker-1",
                current_dispatcher_open=True,
                lease_until=START + timedelta(seconds=120),
            )
        )
        await writer.exec_driver_sql("ANALYZE attempt")

    async with database.transaction() as writer:
        with executed_queries(writer) as statements:
            available = await worker_slot_available(writer, "worker-1", 1, START)
        if database.dialect is Dialect.POSTGRESQL:
            plans = [
                as_json_value(
                    (
                        await writer.exec_driver_sql(
                            "EXPLAIN (ANALYZE, FORMAT JSON) " + sql, args
                        )
                    ).scalar_one()
                )
                for sql, args in statements
            ]
        else:
            plans = [
                str(
                    list(
                        await writer.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, args)
                    )
                )
                for sql, args in statements
            ]

    assert available is False
    assert all("LIMIT" in sql for sql, _ in statements)
    if database.dialect is Dialect.POSTGRESQL:
        nodes = [node for plan in plans for node in plan_nodes(plan)]
        assert all(node["Node Type"] != "Seq Scan" for node in nodes)
        assert {str(node.get("Index Name")) for node in nodes} >= {
            "ix_attempt_worker_open",
            "ix_attempt_worker_unknown",
        }
        assert all(
            type(node["Actual Rows"]) is int and node["Actual Rows"] <= 32
            for node in nodes
        )
        assert all(node.get("Rows Removed by Filter", 0) == 0 for node in nodes)
    else:
        assert all("SCAN attempt" not in str(plan) for plan in plans), plans
        assert all("TEMP B-TREE" not in str(plan) for plan in plans)
