import pytest
from sqlalchemy import update

from kora.db.connection import Database
from kora.db.tables import run
from kora.engine.dependency_rules import RULES
from kora.engine.dependency_settlement import DependencySettlementState
from tests.engine.dependency_settlement_support import dependency_case, dependency_cases
from tests.engine.test_dependency_rules import OUTCOMES, PAIRS


@pytest.mark.asyncio
async def test_parameter_inputs_do_not_block_false_dependency_settlement(
    database: Database,
) -> None:
    case = await dependency_case(database, parameter_input=True)
    root, _, target = case.readiness.nodes

    outcome = await case.settle(database)
    saved = await case.promotion.snapshot(database)

    assert root.predecessors == ()
    assert target.predecessors == ("agent", "producer")
    for node in (root, target):
        parameter = next(entry for entry in node.inputs if entry.name == "requested")
        assert parameter.producer == "$params"
        assert parameter.output is None
        assert parameter.path == "enabled"
    assert outcome is DependencySettlementState.SETTLED
    current = next(row for row in saved[1] if row["node_path"] == "approval")
    assert current["status"] == "upstream_failed"
    assert current["consumed_versions"] == {}
    assert current["current_attempt_id"] is None
    assert current["attempt_count"] == 0
    assert saved[0]["needs_scheduling"] is True
    events = [row for row in saved[2] if row["kind"] == "node.upstream_failed"]
    assert len(events) == 1
    assert events[0]["payload"] == {
        "node_path": "approval",
        "reason": "trigger_rule_unsatisfied",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", RULES)
async def test_failure_propagates_by_trigger_rule(
    database: Database, rule: str
) -> None:
    cases = await dependency_cases(database, rule, PAIRS)
    before = [await case.promotion.snapshot(database) for case in cases]

    results = [await case.settle(database) for case in cases]
    after = [await case.promotion.snapshot(database) for case in cases]

    assert len(cases) == len(PAIRS)
    assert len({case.promotion.run_id for case in cases}) == len(PAIRS)
    assert len({case.promotion.receipt_id for case in cases}) == len(PAIRS)
    assert len(
        {value for case in cases for value in case.node_ids.values()}
    ) == 3 * len(PAIRS)
    for original, saved, result, expected in zip(
        before, after, results, OUTCOMES[rule], strict=True
    ):
        target = next(row for row in saved[1] if row["node_path"] == "approval")
        if expected == "satisfied":
            assert result is DependencySettlementState.UNCHANGED
            assert saved == original
        else:
            assert result is DependencySettlementState.SETTLED
            assert target["status"] == expected
            assert saved[0]["needs_scheduling"] is True
            assert len(saved[2]) == len(original[2]) + 1
        assert target["attempt_count"] == 0
        assert target["consumed_versions"] == {}
        assert target["current_attempt_id"] is None
        assert target["started_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", ["all_success", "one_failed", "all_done"])
async def test_individual_cancellation_is_not_run_cancellation(
    database: Database, rule: str
) -> None:
    individual = await dependency_case(database, rule, ("cancelled", "succeeded"))
    cancelled_run = await dependency_case(database, rule, ("cancelled", "succeeded"))
    async with database.transaction() as writer:
        await writer.execute(
            update(run)
            .where(run.c.id == cancelled_run.promotion.run_id)
            .values(status="cancelled")
        )
    cancelled_before = await cancelled_run.promotion.snapshot(database)

    individual_result = await individual.settle(database)
    run_result = await cancelled_run.settle(database)

    assert individual_result is (
        DependencySettlementState.SETTLED
        if rule == "all_success"
        else DependencySettlementState.UNCHANGED
    )
    assert run_result is DependencySettlementState.REFUSED
    assert await cancelled_run.promotion.snapshot(database) == cancelled_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("states", "kind"),
    [
        (("succeeded", "failed"), "node.upstream_failed"),
        (("skipped", "skipped"), "node.skipped"),
    ],
)
async def test_dependency_outcome_event_matches_state(
    database: Database, states: tuple[str, str], kind: str
) -> None:
    case = await dependency_case(database, states=states)
    before = await case.promotion.snapshot(database)
    case.promotion.clock.advance(1)

    assert await case.settle(database) is DependencySettlementState.SETTLED
    after = await case.promotion.snapshot(database)

    records = [row for row in after[2] if row["kind"] == kind]
    assert len(records) == 1
    record = records[0]
    target = next(row for row in after[1] if row["node_path"] == "approval")
    assert kind == "node." + str(target["status"])
    assert record["payload"] == {
        "node_path": "approval",
        "reason": "trigger_rule_unsatisfied",
    }
    assert record["actor"] == {"kind": "system", "component": "scheduler"}
    assert record["namespace_id"] == case.promotion.scope.namespace_id
    assert record["node_instance_id"] == target["id"]
    assert record["attempt_id"] is None
    assert record["at"] == target["updated_at"] == after[0]["updated_at"]
    assert after[0]["started_at"] == before[0]["started_at"]
    assert [row for row in after[1] if row["node_path"] != "approval"] == [
        row for row in before[1] if row["node_path"] != "approval"
    ]
