from pathlib import Path

import pytest
from sqlalchemy import insert, select, update

from kora.db.admission_waits import due_admission_statement, open_wait_statement
from kora.db.connection import Database, Dialect
from kora.db.tables import wait
from tests.db.seed import START, Seeder
from tests.db.test_expiry_query_work import query_plan
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.admission_wait_support import admission_case


@pytest.mark.asyncio
async def test_admission_refresh_does_not_read_legacy_payloads(
    database: Database,
) -> None:
    case = await admission_case(database)
    async with database.transaction() as writer:
        await case.apply(await case.context(writer))
    [saved] = await case.saved_waits(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(wait)
            .where(wait.c.id == saved["id"])
            .values(
                reason="private-old-reason" * 100_000,
                observation={"private": "old-observation" * 100_000},
            )
        )
        if database.dialect is Dialect.SQLITE:
            await writer.exec_driver_sql(
                "UPDATE wait SET reason = 'invalid-json' WHERE id = ?", (saved["id"],)
            )

    async with database.transaction() as writer:
        context = await case.context(writer)
        with executed_queries(writer) as statements:
            state = await case.apply(context, str(saved["id"]))
    async with database.read_transaction() as reader:
        reason = await reader.scalar(
            select(wait.c.reason).where(wait.c.id == saved["id"])
        )

    assert state.value == "refreshed"
    assert isinstance(reason, dict)
    assert reason["kind"] == "concurrency_limit"
    reads = [
        sql.lower()
        for sql, _ in statements
        if sql.lstrip().lower().startswith("select")
    ]
    wait_reads = [
        sql for sql in reads if "from main.wait" in sql or "from only wait" in sql
    ]
    assert len(wait_reads) == 1
    assert all("reason" not in sql and "observation" not in sql for sql in wait_reads)


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [300, 3000])
async def test_admission_wait_reads_exclude_closed_history(
    database: Database, history: int, tmp_path: Path
) -> None:
    case = await admission_case(database)
    ids: list[str] = []
    async with database.transaction() as writer:
        seed = Seeder(writer)
        for offset in range(0, history + 200, 100):
            values: list[dict[str, object]] = []
            for number in range(offset, offset + 100):
                identity = seed.new_id()
                if number < 125:
                    ids.append(identity)
                values.append(
                    {
                        "id": identity,
                        "node_instance_id": case.binding.node_id,
                        "kind": "admission",
                        "status": "open" if number < 125 else "cancelled",
                        "wake_at": START,
                        "reason": {"retained": "wide" * 1000},
                        "created_at": START,
                        "updated_at": START,
                    }
                )
            await writer.execute(insert(wait), values)
        await writer.exec_driver_sql("ANALYZE wait")

    async with database.read_transaction() as reader:
        with executed_queries(reader) as queries:
            with await reader.execute(
                open_wait_statement(reader.dialect.name, case.binding.node_id)
            ) as rows:
                owners = rows.all()
            with await reader.execute(
                due_admission_statement(reader.dialect.name, START)
            ) as rows:
                first = rows.all()
            with await reader.execute(
                due_admission_statement(
                    reader.dialect.name,
                    START,
                    (first[-1][0], first[-1][1], first[-1][2]),
                )
            ) as rows:
                second = rows.all()
        plans = [
            await query_plan(reader, database.dialect is Dialect.POSTGRESQL, query)
            for query in queries
        ]
    (tmp_path / "wait-query-plans.txt").write_text(
        "\n\n".join("\n".join(plan) for plan in plans)
    )

    assert len(owners) == 2
    assert len(first) == 100
    assert len(second) == 25
    assert [row[2] for row in (*first, *second)] == ids
    for plan in plans:
        if database.dialect is Dialect.SQLITE:
            assert any("COVERING INDEX ix_wait_" in row for row in plan)
            assert not any("SCAN wait" in row for row in plan)
