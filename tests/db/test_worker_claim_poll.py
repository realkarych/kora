import json

import pytest
from sqlalchemy import insert, select, update

from kora.db.connection import Database, Dialect
from kora.db.tables import attempt, worker
from kora.db.types import as_json_value
from kora.db.worker_claim_poll import advance_claim_cursor, claim_candidates
from tests.db.seed import START, Seeder
from tests.db.test_grant_history_reads import plan_nodes
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.test_grant_history import seed


@pytest.mark.asyncio
async def test_claim_page_limits_candidates_before_eligibility(
    database: Database,
) -> None:
    scope = await seed(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        ids = [scope.attempt_id]
        for number in range(2, 66):
            ids.append(await seeder.attempt(scope.node_id, number=number))
        await writer.execute(update(attempt).values(desired_state="cancel"))
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == ids[-1])
            .values(status="cancelled", recovery_required=True)
        )

    async with database.read_transaction() as reader:
        with executed_queries(reader) as statements:
            first = await claim_candidates(reader, "local", None)
            second = await claim_candidates(reader, "local", first[-1])
            final = await claim_candidates(reader, "local", second[-1])

    assert first == tuple(ids[:32])
    assert second == tuple(ids[32:64])
    assert final == (ids[-1],)
    assert all(
        "LIMIT" in sql and "JOIN" not in sql and "desired_state" not in sql
        for sql, _ in statements
    )


@pytest.mark.asyncio
async def test_claim_cursor_compare_includes_generation_and_wrap(
    database: Database,
) -> None:
    scope = await seed(database)
    async with database.transaction() as writer:
        await writer.execute(update(worker).values(claim_cursor_generation=0))

    async with database.transaction() as writer:
        advanced = await advance_claim_cursor(
            writer, "worker-1", None, 0, scope.attempt_id, START
        )
        wrapped = await advance_claim_cursor(
            writer, "worker-1", scope.attempt_id, 1, None, START
        )
        stale = await advance_claim_cursor(
            writer, "worker-1", None, 0, scope.attempt_id, START
        )
        result = (
            await writer.execute(
                select(worker.c.claim_cursor, worker.c.claim_cursor_generation)
            )
        ).one()

    assert (advanced, wrapped, stale) == (True, True, False)
    assert tuple(result) == (None, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("past_prefix", [False, True])
async def test_claim_candidate_native_query_seeks_past_closed_and_foreign_rows(
    database: Database,
    past_prefix: bool,
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
                "runtime": "foreign" if number % 2 else "local",
                "status": ("running" if number % 2 or past_prefix else "succeeded"),
            }
            for number in range(2, 2050)
        ]
        await writer.execute(insert(attempt), rows)
        tail = await seeder.attempt(scope.node_id, number=2050)
        await writer.exec_driver_sql("ANALYZE attempt")
    after = str(rows[-1]["id"]) if past_prefix else None

    async with database.read_transaction() as reader:
        with executed_queries(reader) as statements:
            candidates = await claim_candidates(reader, "local", after)
        [(sql, args)] = statements
        if database.dialect is Dialect.POSTGRESQL:
            plan = as_json_value(
                (
                    await reader.exec_driver_sql(
                        "EXPLAIN (ANALYZE, FORMAT JSON) " + sql, args
                    )
                ).scalar_one()
            )
            nodes = list(plan_nodes(plan))
            assert all(node["Node Type"] != "Seq Scan" for node in nodes)
            assert "ix_attempt_claim_candidate" in {
                str(node.get("Index Name")) for node in nodes
            }, json.dumps(plan, indent=2)
            assert all(
                type(node["Actual Rows"]) is int and node["Actual Rows"] <= 32
                for node in nodes
            )
            assert all(node.get("Rows Removed by Filter", 0) == 0 for node in nodes)
        else:
            plan = str(
                list(await reader.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, args))
            )
            assert "ix_attempt_claim_candidate" in plan
            assert "SCAN attempt" not in plan
            assert "TEMP B-TREE" not in plan

    assert candidates == ((tail,) if past_prefix else (scope.attempt_id, tail))
