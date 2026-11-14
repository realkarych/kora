from dataclasses import replace

import pytest
from sqlalchemy import delete, func, select, update

from kora.db.connection import Database
from kora.db.tables import artifact_version, attempt, node_instance, run, wait
from kora.db.transactions import WriterTransactionError
from kora.engine.dependency_rules import RULES
from kora.engine.dependency_settlement import (
    DependencySettlementState,
    settle_dependencies,
)
from kora.engine.locks import LockError, TransitionLocks
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.dependency_settlement_support import (
    DependencyCase,
    dependency_case,
    dependency_cases,
)
from tests.engine.lock_support import DeliberateRollback
from tests.engine.test_dependency_rules import OUTCOMES, PAIRS


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
@pytest.mark.parametrize(
    "fault", ["missing", "foreign", "kind", "rule", "iteration", "top_level", "unknown"]
)
async def test_missing_or_ineligible_predecessor_is_never_a_root(
    database: Database, fault: str
) -> None:
    case = await dependency_case(database, "all_done")
    async with database.transaction() as writer:
        predecessor = node_instance.c.id == case.node_ids["producer"]
        if fault == "missing":
            await writer.execute(delete(node_instance).where(predecessor))
        else:
            values: dict[str, object] = {}
            if fault == "foreign":
                seed = Seeder(writer)
                values["run_id"] = await seed.run(
                    case.promotion.scope.namespace_id,
                    case.promotion.scope.graph_id,
                    case.promotion.scope.version_id,
                )
            else:
                field, value = {
                    "kind": ("kind", "agent"),
                    "rule": ("trigger_rule", "one_failed"),
                    "iteration": ("subgraph_attempt", 2),
                    "top_level": ("top_level", False),
                    "unknown": ("status", "private-unknown-state"),
                }[fault]
                values[field] = value
            await writer.execute(
                update(node_instance).where(predecessor).values(**values)
            )
    before = await case.promotion.snapshot(database)

    result = await case.settle(database)

    assert result is DependencySettlementState.REFUSED
    assert await case.promotion.snapshot(database) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "namespace",
        "graph",
        "version",
        "digest",
        "target",
        "path",
        "pointer",
        "expiry",
        "state",
    ],
)
async def test_dependency_settlement_requires_the_current_pinned_owner(
    database: Database, fault: str
) -> None:
    case = await dependency_case(database)
    pin, readiness = case.promotion.pin, case.readiness
    node_id, path = case.node_ids["approval"], "approval"
    async with database.transaction() as writer:
        seed = Seeder(writer)
        if fault == "pointer":
            attempt_id = await seed.attempt(node_id)
            await writer.execute(
                update(node_instance)
                .where(node_instance.c.id == node_id)
                .values(current_attempt_id=attempt_id)
            )
        elif fault in ("expiry", "state"):
            values: dict[str, object] = (
                {"deadline_expired_at": case.promotion.clock.now()}
                if fault == "expiry"
                else {"status": "queued"}
            )
            await writer.execute(
                update(node_instance)
                .where(node_instance.c.id == node_id)
                .values(**values)
            )
    before = await case.promotion.snapshot(database)

    async with database.transaction() as writer:
        context = await case.context(writer)
        if fault in ("namespace", "graph", "version"):
            context = replace(
                context,
                scope=replace(context.scope, **{fault + "_id": case.promotion.run_id}),
            )
            if fault != "version":
                with pytest.raises(LockError):
                    await settle_dependencies(context, pin, readiness, node_id, path)
                result = DependencySettlementState.REFUSED
            else:
                result = await settle_dependencies(
                    context, pin, readiness, node_id, path
                )
        else:
            if fault == "digest":
                pin = replace(pin, execution_contract_digest="0" * 64)
            if fault == "target":
                node_id = case.node_ids["agent"]
            if fault == "path":
                path = "unknown"
            result = await settle_dependencies(context, pin, readiness, node_id, path)

    assert result is DependencySettlementState.REFUSED
    assert await case.promotion.snapshot(database) == before


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


@pytest.mark.asyncio
async def test_dependency_settlement_requires_original_transaction_locks(
    database: Database,
) -> None:
    case = await dependency_case(database)
    async with database.transaction() as writer:
        context = await case.context(writer)
    before = await case.promotion.snapshot(database)

    with pytest.raises(WriterTransactionError):
        await settle_dependencies(
            context,
            case.promotion.pin,
            case.readiness,
            case.node_ids["approval"],
            "approval",
        )
    async with database.transaction() as writer:
        context = replace(context, connection=writer, locks=TransitionLocks(writer))
        with pytest.raises(LockError):
            await settle_dependencies(
                context,
                case.promotion.pin,
                case.readiness,
                case.node_ids["approval"],
                "approval",
            )

    assert await case.promotion.snapshot(database) == before
