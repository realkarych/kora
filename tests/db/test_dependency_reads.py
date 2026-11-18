from pathlib import Path

import pytest
from sqlalchemy import insert, select, update

from kora.db.connection import Database, Dialect
from kora.db.dependency_reads import DependencyNodeState, read_dependency_node
from kora.db.tables import node_instance
from kora.engine.dependency_settlement import DependencySettlementState
from tests.db.seed import Seeder
from tests.db.test_expiry_query_work import query_plan
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.dependency_settlement_support import dependency_case


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [0, 3000])
async def test_dependency_reads_are_fixed_scalar_point_queries(
    database: Database, history: int, tmp_path: Path
) -> None:
    case = await dependency_case(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        with await writer.execute(
            select(node_instance).where(node_instance.c.id == case.node_ids["producer"])
        ) as result:
            prototype = dict(result.mappings().one())
        for offset in range(0, history, 100):
            await writer.execute(
                insert(node_instance),
                [
                    {
                        **prototype,
                        "id": seed.new_id(),
                        "node_path": f"history_{number}",
                        "consumed_versions": {"retained": "x" * 4096},
                    }
                    for number in range(offset, offset + 100)
                ],
            )

    async with database.transaction() as writer:
        with executed_queries(writer) as statements:
            outcome = await case.apply(writer)
        reads = [
            query
            for query in statements
            if query[0].startswith("SELECT") and "FROM node_instance" in query[0]
        ]
        plans = [
            await query_plan(writer, database.dialect is Dialect.POSTGRESQL, query)
            for query in reads
        ]
        (tmp_path / "dependency-query-plans.txt").write_text(
            "\n\n".join("\n".join(plan) for plan in plans)
        )

    assert outcome is DependencySettlementState.SETTLED
    assert len(reads) == 3
    for sql, _ in reads:
        assert "node_instance.run_id =" in sql
        assert "node_instance.node_path =" in sql
        assert "consumed_versions" not in sql
        assert "branch_selection" not in sql
        assert "node_instance.status IN" in sql
        assert "node_instance.current_attempt_id IS NULL" in sql
        assert "node_instance.deadline_expired_at IS NULL" in sql
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_dependency_read_never_returns_unknown_status_content(
    database: Database,
) -> None:
    case = await dependency_case(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == case.node_ids["producer"])
            .values(status="private-status-" * 16)
        )

    async with database.read_transaction() as reader:
        unknown = await read_dependency_node(
            reader, case.promotion.run_id, "producer", "command", "all_success"
        )
        pending = await read_dependency_node(
            reader,
            case.promotion.run_id,
            "approval",
            "human_input",
            "all_success",
            node_id=case.node_ids["approval"],
        )

    assert unknown is None
    assert pending == DependencyNodeState("pending", True, True)
    assert database.units_in_flight() == 0
