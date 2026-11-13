import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Dialect
from kora.db.deadline_discovery import DueAttemptSweep
from kora.db.tables import approval_request, attempt, attempt_dispatcher, wait
from kora.engine.expiry import expire_run
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.expiry_support import ExecutionCase, execution_case

Query = tuple[str, tuple[object, ...]]


class RolledBackExpiry(RuntimeError):
    def __init__(self, queries: list[Query]) -> None:
        super().__init__("the measured expiry was rolled back")
        self.queries = queries


async def capture_expiry(database: Database, case: ExecutionCase) -> None:
    async with database.transaction() as writer:
        context = await case.context(writer)
        with executed_queries(writer) as queries:
            await expire_run(context)
        raise RolledBackExpiry(queries)


async def query_plan(
    connection: AsyncConnection, postgres: bool, query: Query
) -> list[str]:
    sql, arguments = query
    prefix = (
        "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) "
        if postgres
        else "EXPLAIN QUERY PLAN "
    )
    savepoint = await connection.begin_nested()
    try:
        with await connection.exec_driver_sql(prefix + sql, arguments) as result:
            return [str(tuple(row)[0] if postgres else tuple(row)[3]) for row in result]
    finally:
        await savepoint.rollback()


async def seed_query_history(
    database: Database, case: ExecutionCase, history: int
) -> None:
    prototype = await case.snapshot(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        foreign = await seed.run(
            case.promotion.scope.namespace_id,
            case.promotion.scope.graph_id,
            case.promotion.scope.version_id,
            "succeeded",
        )
        foreign_node = await seed.node(foreign, "history")
        with await writer.execute(
            select(attempt_dispatcher).where(attempt_dispatcher.c.id == case.grant_id)
        ) as result:
            grant = dict(result.mappings().one())
        for offset in range(0, history, 100):
            attempts: list[dict[str, object]] = []
            waits: list[dict[str, object]] = []
            approvals: list[dict[str, object]] = []
            for number in range(offset + 2, offset + 102):
                attempts.append(
                    {
                        **prototype,
                        "id": seed.new_id(),
                        "number": number,
                        "status": "succeeded",
                    }
                )
                wait_id = seed.new_id()
                waits.append(
                    dict(
                        id=wait_id,
                        node_instance_id=foreign_node,
                        attempt_id=None,
                        kind="approval",
                        wake_at=None,
                        subscription=None,
                        reason=None,
                        status="open" if number % 2 else "cancelled",
                        observation=None,
                        evaluation_error=None,
                        **seed.stamps(),
                    )
                )
                approvals.append(
                    dict(
                        id=seed.new_id(),
                        node_instance_id=foreign_node,
                        wait_id=wait_id,
                        channel="fake",
                        channel_request_id=None,
                        delivery={"desired_state": "closed", "revision": 1},
                        form={},
                        run_cost={},
                        status="cancelled",
                        **seed.stamps(),
                    )
                )
            await writer.execute(insert(attempt), attempts)
            await writer.execute(
                insert(attempt_dispatcher),
                [
                    {
                        **grant,
                        "id": seed.new_id(),
                        "attempt_id": row["id"],
                        "closed_at": case.promotion.clock.now(),
                    }
                    for row in attempts
                ],
            )
            await writer.execute(insert(wait), waits)
            await writer.execute(insert(approval_request), approvals)
        active = [
            {**prototype, "id": seed.new_id(), "number": history + index + 2}
            for index in range(149)
        ]
        await writer.execute(insert(attempt), active)
        await writer.execute(
            update(attempt)
            .where(attempt.c.node_instance_id == case.node_id)
            .values(
                deadline=case.promotion.clock.now(),
                lease_until=case.promotion.clock.now(),
            )
        )
        wait_id = seed.new_id()
        await writer.execute(
            insert(wait).values(
                id=wait_id,
                node_instance_id=case.node_id,
                attempt_id=None,
                kind="approval",
                wake_at=None,
                subscription=None,
                reason=None,
                status="open",
                observation=None,
                evaluation_error=None,
                **seed.stamps(),
            )
        )
        await writer.execute(
            insert(approval_request).values(
                id=seed.new_id(),
                node_instance_id=case.node_id,
                wait_id=wait_id,
                channel="fake",
                channel_request_id=None,
                delivery={"desired_state": "open", "revision": 1},
                form={},
                run_cost={},
                status="pending",
                **seed.stamps(),
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [300, 3000])
async def test_expiry_query_work_is_bounded_for_declared_history_sizes(
    database: Database, history: int, tmp_path: Path
) -> None:
    case = await execution_case(database, timeout="0s")
    await seed_query_history(database, case, history)
    postgres = database.dialect is Dialect.POSTGRESQL
    if postgres:
        async with database.transaction() as writer:
            for table in (
                "attempt",
                "attempt_dispatcher",
                "node_instance",
                "wait",
                "approval_request",
            ):
                await writer.exec_driver_sql("ANALYZE " + table)

    with pytest.raises(RolledBackExpiry) as expiry:
        await capture_expiry(database, case)
    async with database.transaction() as writer:
        with executed_queries(writer) as due_queries:
            found = await DueAttemptSweep("deadline").next_page(
                writer, case.promotion.clock.now() + timedelta(seconds=60)
            )
        due_plan = await query_plan(writer, postgres, due_queries[-1])
        cancellations = [
            query
            for query in expiry.value.queries
            if query[0].startswith("SELECT attempt.id, attempt.node_instance_id")
            and "AS uncertain" in query[0]
        ]
        cancellation_plan = await query_plan(writer, postgres, cancellations[0])
        delivery = next(
            query
            for query in expiry.value.queries
            if query[0].startswith("UPDATE approval_request")
        )
        delivery_plan = await query_plan(writer, postgres, delivery)
        open_waits = next(
            query
            for query in expiry.value.queries
            if query[0].startswith("SELECT wait.id")
        )
        wait_plan = await query_plan(writer, postgres, open_waits)
        remaining = await writer.scalar(
            select(attempt.c.id).where(
                attempt.c.id == case.attempt_id, attempt.c.desired_state == "run"
            )
        )
    (tmp_path / "native-plans.txt").write_text(
        "\n\n".join(
            "\n".join(plan)
            for plan in (due_plan, cancellation_plan, wait_plan, delivery_plan)
        )
    )

    assert len(found) == 100
    assert remaining == case.attempt_id
    owner_queries = [
        arguments for _, arguments in cancellations if case.node_id in arguments
    ]
    assert len(owner_queries) == 4
    assert max(map(len, owner_queries)) <= min(map(len, owner_queries)) + 1
    assert any(
        index in line
        for index in ("ix_attempt_due_deadline", "ix_attempt_lease_until")
        for line in due_plan
    ), "\n".join(due_plan)
    assert any("ix_approval_request_wait" in line for line in delivery_plan), (
        delivery_plan
    )
    assert any("ix_wait_node_status" in line for line in wait_plan), "\n".join(
        wait_plan
    )
    if postgres:
        due_scans = [
            line
            for line in due_plan
            if "ix_attempt_due_deadline" in line or "ix_attempt_lease_until" in line
        ]
        assert len(due_scans) == 1
        assert_scan_work(due_scans[0], rows=150, loops=1)
        assert removed_rows(due_plan) <= 150
        assert_cancellation_work(cancellation_plan, history)
        assert not any("Seq Scan" in line for line in wait_plan + delivery_plan)
    else:
        assert any("ix_attempt_cancel_due" in line for line in cancellation_plan), (
            cancellation_plan
        )
    assert database.units_in_flight() == 0


def assert_scan_work(line: str, *, rows: int, loops: int) -> None:
    match = re.search(r"actual rows=([0-9]+) loops=([0-9]+)", line)
    assert match is not None, line
    assert int(match[1]) <= rows, line
    assert int(match[2]) <= loops, line


def removed_rows(plan: list[str]) -> int:
    return sum(
        int(match[1])
        for line in plan
        if (match := re.search(r"Rows Removed by Filter: ([0-9]+)", line))
    )


def assert_cancellation_work(plan: list[str], history: int) -> None:
    candidates = [line for line in plan if " on attempt attempt_1 (" in line]
    facts = [line for line in plan if " on attempt (" in line]
    grants = [line for line in plan if " on attempt_dispatcher (" in line]
    assert len(candidates) == len(facts) == len(grants) == 1, plan
    assert_scan_work(candidates[0], rows=100, loops=1)
    if history == 300:
        assert any(
            index in candidates[0] for index in ("ix_attempt_cancel_due", "pk_attempt")
        ), candidates
        assert removed_rows(plan) <= 300, plan
    else:
        assert "ix_attempt_cancel_due" in candidates[0], candidates
        assert removed_rows(plan) == 0, plan
    if history == 300 and "Seq Scan" in facts[0]:
        assert_scan_work(facts[0], rows=450, loops=1)
    else:
        assert "Index Scan using pk_attempt" in facts[0], facts
        assert_scan_work(facts[0], rows=1, loops=100)
    assert "Index Scan using uq_attempt_dispatcher_attempt_id_lease_epoch" in grants[0]
    assert_scan_work(grants[0], rows=1, loops=100)
    grant_limit = plan[plan.index(grants[0]) - 1]
    assert "Limit" in grant_limit, plan
    assert_scan_work(grant_limit, rows=1, loops=100)
    other_scans = [
        line
        for line in plan
        if "Scan" in line and line not in candidates + facts + grants
    ]
    assert all('Subquery Scan on "ANY_subquery"' in line for line in other_scans), plan
