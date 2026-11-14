from pathlib import Path

import pytest
from sqlalchemy import insert, update

from kora.db.connection import Database, Dialect
from kora.db.tables import node_instance
from kora.engine.admission_discovery import AdmissionSweep
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_expiry_query_work import query_plan
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.promotion_support import append_pending, promotion_case


@pytest.mark.parametrize("size", [0, -1, 101, True])
def test_admission_discovery_refuses_invalid_page(size: int) -> None:
    with pytest.raises(ValueError, match="admission page"):
        AdmissionSweep(size)


@pytest.mark.asyncio
async def test_admission_discovery_orders_nodes_across_promoted_runs(
    database: Database,
) -> None:
    first = await promotion_case(database)
    second = await append_pending(database, first)
    await first.promote(database)
    await second.promote(database)
    ordered: list[str] = []
    async with database.transaction() as writer:
        for path in ("agent", "command"):
            for case in (second, first):
                identity = first.identifiers.new()
                await writer.execute(
                    update(node_instance)
                    .where(
                        node_instance.c.run_id == case.run_id,
                        node_instance.c.kind == path,
                    )
                    .values(id=identity)
                )
                ordered.append(identity)
    ledger = ConnectionLedger(database)
    sweep = AdmissionSweep(3)

    async with database.read_transaction() as reader:
        a = await sweep.next_page(reader)
    async with database.read_transaction() as reader:
        b = await sweep.next_page(reader)
    async with database.read_transaction() as reader:
        restarted = await AdmissionSweep().next_page(reader)

    assert a.node_ids + b.node_ids == tuple(ordered)
    assert a.wrapped
    assert not b.wrapped
    assert a.ceiling == ordered[-1]
    assert b.ceiling is None
    assert restarted.node_ids == tuple(ordered)
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", ["retained", "terminal", "deleted"])
async def test_admission_discovery_rotates_past_arrivals_and_changed_eligibility(
    database: Database, tail: str
) -> None:
    case = await promotion_case(database)
    await case.promote(database)
    async with database.transaction() as writer:
        await writer.execute(update(node_instance).values(status="succeeded"))
        seed = Seeder(writer)
        behind = await seed.node(case.run_id, "behind", "succeeded")
        first = await seed.node(case.run_id, "first")
        last = await seed.node(case.run_id, "last")
    sweep = AdmissionSweep(1)
    async with database.read_transaction() as reader:
        page = await sweep.next_page(reader)

    async with database.transaction() as writer:
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == behind)
            .values(status="pending")
        )
        arrival = await Seeder(writer).node(case.run_id, "arrival")
        if tail == "terminal":
            await writer.execute(
                update(node_instance)
                .where(node_instance.c.id == last)
                .values(status="succeeded")
            )
        elif tail == "deleted":
            await writer.execute(
                node_instance.delete().where(node_instance.c.id == last)
            )
    async with database.read_transaction() as reader:
        tail_page = await sweep.next_page(reader)
        if tail == "retained":
            exhausted = await sweep.next_page(reader)
            assert exhausted.node_ids == ()
            assert exhausted.ceiling is None
        wrapped = await sweep.next_page(reader)
        next_first = await sweep.next_page(reader)
        rest = await sweep.next_page(reader)
        if tail == "retained":
            rest = await sweep.next_page(reader)

    assert page.node_ids == (first,)
    assert page.ceiling == last
    assert tail_page.node_ids == ((last,) if tail == "retained" else ())
    assert wrapped.wrapped
    assert wrapped.node_ids == (behind,)
    assert next_first.node_ids == (first,)
    assert rest.node_ids == (arrival,)
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_admission_discovery_excludes_noninitial_physical_nodes(
    database: Database,
) -> None:
    case = await promotion_case(database)
    await case.promote(database)
    async with database.transaction() as writer:
        await writer.execute(update(node_instance).values(status="succeeded"))
        seed = Seeder(writer)
        rows = [
            seed.node_values(case.run_id, str(number), "pending", True)
            for number in range(10)
        ]
        changes: tuple[dict[str, object], ...] = (
            {"status": "queued"},
            {"status": "succeeded"},
            {"kind": "human_input"},
            {"top_level": False},
            {"subgraph_attempt": 2},
            {"attempt_count": 1},
            {"current_attempt_id": seed.new_id()},
            {"status": "waiting"},
            {"status": "ready"},
            {},
        )
        for row, changed in zip(rows, changes, strict=True):
            row.update(changed)
        await writer.execute(insert(node_instance), rows)

    async with database.read_transaction() as reader:
        found = await AdmissionSweep().next_page(reader)

    assert found.node_ids == tuple(str(row["id"]) for row in rows[-3:])


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [300, 3000])
async def test_admission_discovery_bounds_live_pages_with_completed_history(
    database: Database, history: int, tmp_path: Path
) -> None:
    case = await promotion_case(database)
    await case.promote(database)
    expected: list[str] = []
    async with database.transaction() as writer:
        await writer.execute(update(node_instance).values(status="succeeded"))
        seed = Seeder(writer)
        for start in range(0, history + 200, 100):
            rows = [
                seed.node_values(
                    case.run_id, str(i), "succeeded" if i < history else "pending", True
                )
                for i in range(start, start + 100)
            ]
            expected.extend(
                str(row["id"]) for row in rows if row["status"] == "pending"
            )
            await writer.execute(insert(node_instance), rows)
        await writer.exec_driver_sql("ANALYZE node_instance")
    ledger = ConnectionLedger(database)
    sweep = AdmissionSweep()

    async with database.read_transaction() as reader:
        with executed_queries(reader) as queries:
            first = await sweep.next_page(reader)
            second = await sweep.next_page(reader)
        plans = [
            await query_plan(reader, database.dialect is Dialect.POSTGRESQL, query)
            for query in queries
        ]
    (tmp_path / "admission-query-plans.txt").write_text(
        "\n\n".join("\n".join(plan) for plan in plans)
    )

    assert len(first.node_ids) == len(second.node_ids) == 100
    assert first.node_ids + second.node_ids == tuple(expected)
    assert len(queries) == 3
    assert ledger.outstanding == database.units_in_flight() == 0
