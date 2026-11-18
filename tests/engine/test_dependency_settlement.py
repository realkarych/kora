import pytest
from sqlalchemy import update

from kora.db.connection import Database
from kora.db.tables import run
from kora.engine.dependency_rules import RULES
from kora.engine.dependency_settlement import DependencySettlementState
from tests.engine.dependency_settlement_support import dependency_case, dependency_cases
from tests.engine.test_dependency_rules import OUTCOMES, PAIRS

REQUIRED_GATE_OUTCOMES = (
    "satisfied",
    "upstream_failed",
    "skipped",
    "skipped",
    "skipped",
    "upstream_failed",
)


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
    for original, saved, result, trigger, required in zip(
        before, after, results, OUTCOMES[rule], REQUIRED_GATE_OUTCOMES, strict=True
    ):
        expected = required if trigger == "satisfied" else trigger
        target = next(row for row in saved[1] if row["node_path"] == "approval")
        if expected == "satisfied":
            assert result is DependencySettlementState.UNCHANGED
            assert saved == original
        else:
            assert result is DependencySettlementState.SETTLED
            assert target["status"] == expected
            assert saved[0]["needs_scheduling"] is True
            assert len(saved[2]) == len(original[2]) + 1
            records = [row for row in saved[2] if row["kind"] == "node." + expected]
            assert len(records) == 1
            assert records[0]["payload"] == {
                "node_path": "approval",
                "reason": "required_input_unavailable"
                if trigger == "satisfied"
                else "trigger_rule_unsatisfied",
            }
        assert target["attempt_count"] == 0
        assert target["consumed_versions"] == {}
        assert target["current_attempt_id"] is None
        assert target["started_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rule", "states", "required", "expected"),
    [
        ("all_done", ("failed", "succeeded"), ("producer",), "pending"),
        ("all_done", ("succeeded", "failed"), ("producer",), "upstream_failed"),
        ("one_failed", ("succeeded", "cancelled"), ("producer",), "upstream_failed"),
        ("all_done", ("skipped", "failed"), ("agent", "producer"), "upstream_failed"),
        ("all_done", ("failed", "skipped"), ("agent", "producer"), "upstream_failed"),
        ("all_done", ("skipped", "skipped"), ("agent", "producer"), "skipped"),
        ("one_failed", ("failed", "skipped"), ("producer",), "skipped"),
        ("none_failed", ("succeeded", "skipped"), ("producer",), "skipped"),
        (
            "none_failed_min_one_success",
            ("succeeded", "skipped"),
            ("producer",),
            "skipped",
        ),
        ("all_skipped", ("skipped", "skipped"), ("producer",), "skipped"),
        ("one_failed", ("failed", "succeeded"), ("producer",), "pending"),
        ("all_done", ("cancelled", "upstream_failed"), (), "pending"),
    ],
)
async def test_required_inputs_settle_only_after_satisfied_trigger(
    database: Database,
    rule: str,
    states: tuple[str, str],
    required: tuple[str, ...],
    expected: str,
) -> None:
    case = await dependency_case(database, rule, states, required_producers=required)
    before = await case.promotion.snapshot(database)

    result = await case.settle(database)
    after = await case.promotion.snapshot(database)

    target = next(row for row in after[1] if row["node_path"] == "approval")
    assert target["status"] == expected
    assert target["consumed_versions"] == {}
    assert target["current_attempt_id"] is None
    assert target["attempt_count"] == 0
    assert target["started_at"] is None
    if expected == "pending":
        assert result is DependencySettlementState.UNCHANGED
        assert after == before
    else:
        assert result is DependencySettlementState.SETTLED
        assert len(after[2]) == len(before[2]) + 1
        records = [row for row in after[2] if row["kind"] == "node." + expected]
        assert len(records) == 1
        assert records[0]["payload"] == {
            "node_path": "approval",
            "reason": "required_input_unavailable",
        }
        assert after[0]["needs_scheduling"] is True


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
