import pytest
from sqlalchemy import func, select, update

from kora.db.connection import Database
from kora.db.tables import artifact_version, attempt, node_instance, run, wait
from kora.engine.dependency_rules import RULES
from kora.engine.dependency_settlement import DependencySettlementState
from tests.db.closing import ConnectionLedger
from tests.engine.dependency_settlement_support import DependencyCase, dependency_case
from tests.engine.lock_support import DeliberateRollback


async def rolled_back_settlement(database: Database, case: DependencyCase) -> None:
    async with database.transaction() as writer:
        assert await case.apply(writer) is DependencySettlementState.SETTLED
        assert (
            await writer.scalar(
                select(node_instance.c.status).where(
                    node_instance.c.id == case.node_ids["approval"]
                )
            )
            == "upstream_failed"
        )
        raise DeliberateRollback()


@pytest.mark.asyncio
async def test_dependency_settlement_is_atomic_and_replay_safe(
    database: Database,
) -> None:
    case = await dependency_case(database)
    ledger = ConnectionLedger(database)
    before = await case.promotion.snapshot(database)

    with pytest.raises(DeliberateRollback):
        await rolled_back_settlement(database, case)
    rolled_back = await case.promotion.snapshot(database)
    assert await case.settle(database) is DependencySettlementState.SETTLED
    committed = await case.promotion.snapshot(database)
    case.promotion.clock.advance(5)
    replay = await case.settle(database)

    assert rolled_back == before
    assert replay is DependencySettlementState.UNCHANGED
    assert await case.promotion.snapshot(database) == committed
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", RULES)
async def test_dependency_rule_never_supplies_input_authority(
    database: Database, rule: str
) -> None:
    case = await dependency_case(database, rule, ("pending", "pending"))
    before = await case.promotion.snapshot(database)

    root = await case.settle(database, "agent")
    waiting = await case.settle(database)

    assert root is waiting is DependencySettlementState.UNCHANGED
    assert await case.promotion.snapshot(database) == before
    async with database.read_transaction() as reader:
        for table in (attempt, wait, artifact_version):
            assert await reader.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", ["all_success", "all_done", "one_failed"])
async def test_run_deadline_wins_at_exact_equality(
    database: Database, rule: str
) -> None:
    case = await dependency_case(database, rule)
    async with database.transaction() as writer:
        await writer.execute(
            update(run)
            .where(run.c.id == case.promotion.run_id)
            .values(limits={"timeout": "01s"})
        )
    case.promotion.clock.advance(1)

    result = await case.settle(database)
    after = await case.promotion.snapshot(database)

    assert result is DependencySettlementState.EXPIRED
    assert after[0]["deadline_expired_at"] is not None
    assert after[0]["needs_scheduling"] is True
    assert (
        next(row for row in after[1] if row["node_path"] == "approval")["status"]
        == "pending"
    )
    assert not any(str(row["kind"]).startswith("node.") for row in after[2])
    case.promotion.clock.advance(-2)
    assert await case.settle(database) is DependencySettlementState.EXPIRED
