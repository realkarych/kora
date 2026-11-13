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
async def test_expiry_queries_exclude_finished_and_foreign_history(
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
    assert any("ix_attempt_cancel_due" in line for line in cancellation_plan), (
        cancellation_plan
    )
    assert any("ix_approval_request_wait" in line for line in delivery_plan), (
        delivery_plan
    )
    assert any("ix_wait_node_status" in line for line in wait_plan), "\n".join(
        wait_plan
    )
    if postgres:
        for plan, indexes in (
            (due_plan, ("ix_attempt_due_deadline", "ix_attempt_lease_until")),
            (cancellation_plan, ("ix_attempt_cancel_due",)),
        ):
            scans = [line for line in plan if any(index in line for index in indexes)]
            assert len(scans) == 1
            match = re.search(r"actual rows=([0-9]+) loops=([0-9]+)", scans[0])
            assert match is not None
            assert int(match[1]) <= 150
            assert int(match[2]) == 1
            removed = [
                int(match[1])
                for line in plan
                if (match := re.search(r"Rows Removed by Filter: ([0-9]+)", line))
            ]
            assert sum(removed) <= 150
        assert not any("Seq Scan" in line for line in wait_plan + delivery_plan)
    assert database.units_in_flight() == 0
