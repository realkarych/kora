from pathlib import Path

import pytest

from kora.db.approval_cost_reads import (
    COST_BATCH_ROWS,
    cost_attempt_statement,
    cost_rows,
    cost_usage_statement,
)
from kora.db.connection import Database, Dialect
from kora.engine.approval_cost import calculate_approval_cost
from kora.engine.locks import TransitionLocks
from tests.db.seed import START
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.approval_cost_native import native_case


@pytest.mark.asyncio
async def test_approval_cost_streams_only_owned_history(
    database: Database, tmp_path: Path
) -> None:
    case = await native_case(database)
    plans: list[str] = []
    counts: list[int] = []
    batches: list[int] = []
    prefix = (
        "EXPLAIN QUERY PLAN "
        if database.dialect is Dialect.SQLITE
        else "EXPLAIN (ANALYZE, BUFFERS) "
    )

    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(case.run_id)
        with executed_queries(writer) as statements:
            snapshot = await calculate_approval_cost(
                writer, locks, case.namespace_id, case.run_id, START
            )
        sources = [
            (query, parameters)
            for query, parameters in statements
            if "FROM attempt JOIN node_instance" in query
            or "FROM usage_record JOIN attempt" in query
        ]
        for index, (query, parameters) in enumerate(sources):
            with await writer.exec_driver_sql(prefix + query, parameters) as result:
                plan = "\n".join(str(tuple(row)) for row in result)
            plans.append(plan)
            (tmp_path / f"cost-source-{index}.sql").write_text(query)
            (tmp_path / f"cost-source-{index}-plan.txt").write_text(plan)
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(case.run_id)
        for statement in (
            cost_attempt_statement(writer, case.namespace_id, case.run_id),
            cost_usage_statement(writer, case.namespace_id, case.run_id),
        ):
            count = 0
            async with cost_rows(writer, statement) as rows:
                while batch := await rows.fetchmany(COST_BATCH_ROWS):
                    count += len(batch)
                    batches.append(len(batch))
            counts.append(count)

    assert len(sources) == 2
    assert counts == [130, 260]
    assert max(batches) == 128
    assert batches == [128, 2, 128, 128, 4]
    assert snapshot.usd_estimate == snapshot.known_usd_estimate == "26"
    assert snapshot.completeness == "complete"
    assert [model.input_tokens for model in snapshot.models] == [390, 390]
    for query, parameters in sources:
        assert case.namespace_id in parameters
        assert case.run_id in parameters
        assert "ORDER BY" not in query
        assert "SUM(" not in query.upper()
        assert all(
            field not in query
            for field in (
                "usage_record.source",
                "attempt.execution_request",
                "attempt.recovery,",
                "price_table",
            )
        )
    for plan in plans:
        if database.dialect is Dialect.POSTGRESQL:
            assert "Index" in plan
            assert "Seq Scan on attempt" not in plan
            assert "Seq Scan on usage_record" not in plan
            assert "Seq Scan on node_instance" not in plan
        else:
            assert "SEARCH attempt USING INDEX" in plan
            assert "SEARCH node_instance USING" in plan
            assert "SCAN usage_record" not in plan
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
