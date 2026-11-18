import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import insert, update

from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.db.reads import execution_run_usage, namespace_outstanding_usage
from kora.db.tables import attempt, event, node_instance
from kora.engine.expiry import expire_run, observe_run_expiry
from kora.engine.expiry_context import ExpiryState
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.locks import LockError
from kora.engine.run_settlement import (
    RunSettlementFailed,
    RunSettlementState,
    settle_run,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder, fixed_randomness
from tests.engine.expiry_support import execution_case, expiry_context
from tests.engine.promotion_support import promotion_case
from tests.engine.settlement_support import finished_run, settlement_context


@pytest.mark.asyncio
@pytest.mark.parametrize("promoted", [False, True])
async def test_run_settlement_requires_finished_top_level_nodes(
    database: Database, promoted: bool
) -> None:
    case = await promotion_case(database)
    if promoted:
        await case.promote(database)
    before = await case.snapshot(database)

    async with database.transaction() as writer:
        result = await settle_run(await settlement_context(writer, case))
    after = await case.snapshot(database)

    assert result is RunSettlementState.UNCHANGED
    assert before == after


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["succeeded", "skipped", "failed", "upstream_failed", "cancelled"]
)
async def test_run_settlement_commits_exact_outcome_and_failure_summary(
    database: Database, status: str
) -> None:
    case = await finished_run(database, status)
    expected = "succeeded" if status in ("succeeded", "skipped") else "failed"
    before, nodes, _ = await case.snapshot(database)
    case.clock.advance(5)

    async with database.transaction() as writer:
        result = await settle_run(await settlement_context(writer, case))
    saved, after_nodes, events = await case.snapshot(database)

    assert result is RunSettlementState.SETTLED
    assert saved["status"] == expected
    assert saved["finished_at"] == case.clock.now()
    assert saved["needs_scheduling"] is True
    assert saved["started_at"] == before["started_at"]
    assert after_nodes == nodes
    [outcome] = [row for row in events if row["kind"] == f"run.{expected}"]
    expected_payload: dict[str, object] = {
        "trigger": before["trigger"],
        "graph_version": case.scope.version_id,
        "rerun_of": None,
    }
    if expected == "failed":
        expected_payload["failed_nodes"] = {
            "count": len(nodes),
            "sample": [
                {"node_path": row["node_path"], "state": status}
                for row in sorted(nodes, key=lambda row: str(row["node_path"]))
            ],
        }
    assert outcome["payload"] == expected_payload
    assert outcome["at"] == saved["finished_at"]
    assert outcome["actor"] == {"kind": "system", "component": "scheduler"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["created", "claimed", "submitted", "running", "finishing"]
)
async def test_run_finish_retains_unresolved_attempt_occupancy(
    database: Database, status: str
) -> None:
    execution = await execution_case(database)
    case = execution.promotion
    async with database.transaction() as writer:
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.run_id == case.run_id)
            .values(status="succeeded", current_attempt_id=None)
        )
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == execution.attempt_id)
            .values(status=status, execution_state="unknown", recovery_required=True)
        )
    before = await execution.snapshot(database)

    async with database.transaction() as writer:
        result = await settle_run(await settlement_context(writer, case, target=False))
    saved, _, _ = await case.snapshot(database)
    after = await execution.snapshot(database)
    async with database.read_transaction() as reader:
        executing = await execution_run_usage(reader, case.scope.graph_id)
        outstanding = await namespace_outstanding_usage(reader, case.scope.namespace_id)

    assert result is RunSettlementState.SETTLED
    assert saved["status"] == "succeeded"
    assert saved["target_held_since"] is not None
    assert executing == outstanding == 1
    assert before == after


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_settled_run_releases_target_once(
    database: Database, status: str
) -> None:
    case = await finished_run(database, status, held=True)

    async with database.transaction() as writer:
        first = await settle_run(await settlement_context(writer, case))
    settled = await case.snapshot(database)
    async with database.transaction() as writer:
        replay = await settle_run(await settlement_context(writer, case, target=False))
    replayed = await case.snapshot(database)
    async with database.read_transaction() as reader:
        outstanding = await namespace_outstanding_usage(reader, case.scope.namespace_id)

    assert first is RunSettlementState.SETTLED
    assert replay is RunSettlementState.UNCHANGED
    assert settled == replayed
    assert settled[0]["target_key"] == "held"
    assert settled[0]["target_held_since"] is None
    assert outstanding == 0


@pytest.mark.asyncio
async def test_run_finish_releases_target_after_the_independent_expiry_pass(
    database: Database,
) -> None:
    case = await finished_run(database, "waiting", held=True, timeout="0s")
    async with database.transaction() as writer:
        await expire_run(await expiry_context(writer, case))
    expired, _, original_events = await case.snapshot(database)
    case.clock.advance(5)

    async with database.transaction() as writer:
        result = await settle_run(await settlement_context(writer, case))
    released = await case.snapshot(database)
    async with database.transaction() as writer:
        replay = await settle_run(await settlement_context(writer, case, target=False))
    repeated = await case.snapshot(database)
    async with database.read_transaction() as reader:
        outstanding = await namespace_outstanding_usage(reader, case.scope.namespace_id)

    assert expired["target_held_since"] is not None
    assert result is RunSettlementState.SETTLED
    assert replay is RunSettlementState.UNCHANGED
    assert released == repeated
    assert released[0]["finished_at"] == expired["finished_at"]
    assert released[0]["target_key"] == "held"
    assert released[0]["target_held_since"] is None
    assert released[2] == original_events
    assert outstanding == 0


@pytest.mark.asyncio
async def test_run_finish_requires_original_target_lock_before_release(
    database: Database,
) -> None:
    case = await finished_run(database, held=True)
    before = await case.snapshot(database)

    with pytest.raises(LockError):
        async with database.transaction() as writer:
            await settle_run(await settlement_context(writer, case, target=False))
    after = await case.snapshot(database)

    assert before == after
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_run_settlement_refuses_a_foreign_scope_without_mutations(
    database: Database,
) -> None:
    case = await finished_run(database, held=True)
    before = await case.snapshot(database)

    async def foreign_finish() -> None:
        async with database.transaction() as writer:
            context = await settlement_context(writer, case)
            foreign = GraphResourceScope(
                case.scope.namespace_id, case.scope.graph_id, case.run_id
            )
            await settle_run(replace(context, scope=foreign))

    with pytest.raises(RunSettlementFailed) as caught:
        await foreign_finish()
    after = await case.snapshot(database)

    assert before == after
    assert str(caught.value) == "run settlement failed"
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_run_finish_preserves_unbounded_accepted_timeout_spelling(
    database: Database,
) -> None:
    case = await finished_run(database, timeout="9" * 5000 + "d")
    case.clock.advance(1_000_000)

    async with database.transaction() as writer:
        result = await settle_run(await settlement_context(writer, case))
    saved, _, _ = await case.snapshot(database)

    assert result is RunSettlementState.SETTLED
    assert saved["status"] == "succeeded"
    assert saved["deadline_expired_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("crossing", [False, True])
async def test_run_finish_rechecks_deadline(database: Database, crossing: bool) -> None:
    case = await finished_run(database, held=True, timeout="60s")
    cutoff = case.clock.now() + timedelta(seconds=60)
    before = cutoff - timedelta(microseconds=1)
    samples = (
        iter((before, cutoff, before, before))
        if crossing
        else iter((cutoff, before, before))
    )

    async with database.transaction() as writer:
        context = replace(
            await settlement_context(writer, case), clock=lambda: next(samples)
        )
        result = await settle_run(context)
    saved, _, events = await case.snapshot(database)

    assert result is RunSettlementState.EXPIRED
    assert saved["status"] == "failed"
    assert saved["deadline_expired_at"] == cutoff
    assert saved["target_held_since"] is None
    [failed] = [row for row in events if row["kind"] == "run.failed"]
    assert isinstance(failed["payload"], dict)
    assert failed["payload"]["deadline"] == {"scope": "run", "reason": "timeout"}
    assert "failed_nodes" not in failed["payload"]
    assert not any(row["kind"] == "run.succeeded" for row in events)


@pytest.mark.asyncio
async def test_run_finish_preserves_a_previously_latched_deadline(
    database: Database,
) -> None:
    case = await finished_run(database, timeout="0s")
    async with database.transaction() as writer:
        observed = await observe_run_expiry(await expiry_context(writer, case, False))
    case.clock.advance(-60)

    async with database.transaction() as writer:
        result = await settle_run(await settlement_context(writer, case))
    saved, _, _ = await case.snapshot(database)

    assert observed is ExpiryState.EXPIRED
    assert result is RunSettlementState.EXPIRED
    assert saved["status"] == "failed"


@pytest.mark.asyncio
async def test_run_settlement_replay_and_competing_writers(database: Database) -> None:
    case = await finished_run(database, held=True)
    started = asyncio.Event()

    async def contender() -> RunSettlementState:
        started.set()
        async with database.transaction() as writer:
            return await settle_run(await settlement_context(writer, case))

    task: asyncio.Task[RunSettlementState] | None = None
    try:
        async with database.transaction() as writer:
            context = await settlement_context(writer, case)
            task = asyncio.create_task(contender())
            await asyncio.wait_for(started.wait(), 5)
            first = await settle_run(context)
        second = await asyncio.wait_for(task, 10)
    finally:
        if task is not None and not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    saved, _, events = await case.snapshot(database)

    assert first is RunSettlementState.SETTLED
    assert second is RunSettlementState.UNCHANGED
    assert saved["target_held_since"] is None
    assert sum(row["kind"] == "run.succeeded" for row in events) == 1


@pytest.mark.asyncio
async def test_run_settlement_rolls_back_status_event_marker_and_target(
    database: Database,
) -> None:
    case = await finished_run(database, held=True)
    case.clock.advance(1)
    generator = IdentifierGenerator(case.clock.now, fixed_randomness(1000))
    duplicate = IdentifierGenerator(case.clock.now, fixed_randomness(1000)).new()
    async with database.transaction() as writer:
        await writer.execute(
            insert(event).values(
                id=duplicate,
                namespace_id=case.scope.namespace_id,
                run_id=case.run_id,
                kind="run.created",
                actor={},
                payload={"private": "retained-value"},
                at=case.clock.now(),
                **Seeder(writer).stamps(),
            )
        )
    before = await case.snapshot(database)
    ledger = ConnectionLedger(database)

    async def fail_event_insert() -> None:
        async with database.transaction() as writer:
            context = replace(
                await settlement_context(writer, case), identifiers=generator
            )
            await settle_run(context)

    with pytest.raises(RunSettlementFailed) as caught:
        await fail_event_insert()
    after = await case.snapshot(database)

    assert str(caught.value) == "run settlement failed"
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert before == after
    assert database.units_in_flight() == ledger.outstanding == 0
    assert ledger.opened == ledger.closed


@pytest.mark.asyncio
async def test_cancelled_settlement_transaction_releases_all_owned_resources(
    database: Database,
) -> None:
    case = await finished_run(database, held=True)
    before = await case.snapshot(database)
    applied = asyncio.Event()
    hold = asyncio.Event()
    ledger = ConnectionLedger(database)

    async def finish_until_cancelled() -> None:
        async with database.transaction() as writer:
            await settle_run(await settlement_context(writer, case))
            applied.set()
            await hold.wait()

    task = asyncio.create_task(finish_until_cancelled())
    try:
        await asyncio.wait_for(applied.wait(), 10)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    after = await case.snapshot(database)
    async with database.transaction() as writer:
        retried = await settle_run(await settlement_context(writer, case))

    assert before == after
    assert database.units_in_flight() == ledger.outstanding == 0
    assert retried is RunSettlementState.SETTLED
