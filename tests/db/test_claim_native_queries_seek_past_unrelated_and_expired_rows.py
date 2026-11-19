from datetime import timedelta

import pytest
from sqlalchemy import insert, select, update

from kora.db.connection import Database, Dialect
from kora.db.tables import attempt
from kora.db.types import as_json_value
from kora.db.worker_claims import (
    worker_slot_available,
)
from tests.db.seed import START, Seeder
from tests.db.test_grant_history_reads import plan_nodes
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.test_grant_history import seed


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
