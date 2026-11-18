import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import insert, select, update

from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.db.reads import has_physical_attempt
from kora.db.tables import attempt, event, node_instance, run
from kora.engine.dependency_settlement import (
    DependencySettlementFailed,
    DependencySettlementState,
    settle_dependencies,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder, fixed_randomness
from tests.engine.dependency_settlement_support import dependency_case
from tests.engine.lock_support import DEADLINE, lock_inventory, wait_for_lock_waiter


@pytest.mark.asyncio
async def test_competing_dependency_settlements_emit_one_event(
    database: Database,
) -> None:
    case = await dependency_case(database)
    ledger = ConnectionLedger(database)
    task: asyncio.Task[DependencySettlementState] | None = None

    try:
        async with database.transaction() as writer:
            context = await case.context(writer)
            task = asyncio.create_task(case.settle(database))
            await wait_for_lock_waiter(database)
            first = await settle_dependencies(
                context,
                case.promotion.pin,
                case.readiness,
                case.node_ids["approval"],
                "approval",
            )
        second = await asyncio.wait_for(task, DEADLINE)
    finally:
        if task is not None and not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    saved = await case.promotion.snapshot(database)

    assert first is DependencySettlementState.SETTLED
    assert second is DependencySettlementState.UNCHANGED
    assert sum(row["kind"] == "node.upstream_failed" for row in saved[2]) == 1
    assert ledger.outstanding == database.units_in_flight() == 0
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_dependency_settlement_observes_clear_committed_before_its_lock(
    database: Database,
) -> None:
    case = await dependency_case(database)
    ledger = ConnectionLedger(database)
    task: asyncio.Task[DependencySettlementState] | None = None

    try:
        async with database.transaction() as writer:
            await case.context(writer)
            await writer.execute(
                update(node_instance)
                .where(node_instance.c.id == case.node_ids["producer"])
                .values(status="pending")
            )
            task = asyncio.create_task(case.settle(database))
            await wait_for_lock_waiter(database)
        outcome = await asyncio.wait_for(task, DEADLINE)
    finally:
        if task is not None and not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    saved = await case.promotion.snapshot(database)

    assert outcome is DependencySettlementState.UNCHANGED
    assert not any(str(row["kind"]).startswith("node.") for row in saved[2])
    assert (
        next(row for row in saved[1] if row["node_path"] == "approval")["status"]
        == "pending"
    )
    assert ledger.outstanding == database.units_in_flight() == 0
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_cancellation_rolls_back_dependency_settlement(
    database: Database,
) -> None:
    case = await dependency_case(database)
    before = await case.promotion.snapshot(database)
    applied, hold = asyncio.Event(), asyncio.Event()
    ledger = ConnectionLedger(database)

    async def hold_prepared() -> None:
        async with database.transaction() as writer:
            await case.apply(writer)
            applied.set()
            await hold.wait()

    task = asyncio.create_task(hold_prepared())
    try:
        await asyncio.wait_for(applied.wait(), DEADLINE)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    after = await case.promotion.snapshot(database)
    retry = await case.settle(database)

    assert after == before
    assert retry is DependencySettlementState.SETTLED
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_failed_dependency_event_rolls_back_state_and_marker(
    database: Database,
) -> None:
    case = await dependency_case(database)
    case.promotion.clock.advance(1)
    identifiers = IdentifierGenerator(case.promotion.clock.now, fixed_randomness(1000))
    duplicate = IdentifierGenerator(
        case.promotion.clock.now, fixed_randomness(1000)
    ).new()
    async with database.transaction() as writer:
        await writer.execute(
            insert(event).values(
                id=duplicate,
                namespace_id=case.promotion.scope.namespace_id,
                run_id=case.promotion.run_id,
                kind="run.created",
                actor={},
                payload={"private": "retained-private-material"},
                at=case.promotion.clock.now(),
                **Seeder(writer).stamps(),
            )
        )
    before = await case.promotion.snapshot(database)
    ledger = ConnectionLedger(database)

    async def collide() -> None:
        async with database.transaction() as writer:
            context = replace(await case.context(writer), identifiers=identifiers)
            await settle_dependencies(
                context,
                case.promotion.pin,
                case.readiness,
                case.node_ids["approval"],
                "approval",
            )

    with pytest.raises(DependencySettlementFailed) as caught:
        await collide()

    assert str(caught.value) == "dependency settlement failed"
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert await case.promotion.snapshot(database) == before
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_logical_dependency_failure_preserves_physical_occupancy(
    database: Database,
) -> None:
    case = await dependency_case(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        attempt_id = await seed.attempt(case.node_ids["producer"], "finishing")
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == attempt_id)
            .values(recovery_required=True, execution_state="unknown")
        )
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == case.node_ids["producer"])
            .values(current_attempt_id=attempt_id)
        )
        await writer.execute(
            update(run)
            .where(run.c.id == case.promotion.run_id)
            .values(target_key="held", target_held_since=case.promotion.clock.now())
        )
        with await writer.execute(
            select(attempt).where(attempt.c.id == attempt_id)
        ) as result:
            original_attempt = dict(result.mappings().one())

    outcome = await case.settle(database)

    assert outcome is DependencySettlementState.SETTLED
    async with database.read_transaction() as reader:
        assert await has_physical_attempt(reader, case.promotion.run_id)
        with await reader.execute(
            select(attempt).where(attempt.c.id == attempt_id)
        ) as result:
            assert dict(result.mappings().one()) == original_attempt
        assert (
            await reader.scalar(
                select(run.c.target_held_since).where(run.c.id == case.promotion.run_id)
            )
            is not None
        )
