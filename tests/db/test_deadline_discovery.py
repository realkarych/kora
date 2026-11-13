from datetime import timedelta

import pytest
from sqlalchemy import select, update

from kora.db.connection import Database
from kora.db.deadline_discovery import DueAttemptSweep, RunningRunSweep
from kora.db.tables import attempt, node_instance, run
from kora.engine.lease_recovery import recover_expired_lease
from tests.db.seed import Seeder
from tests.engine.expiry_support import execution_case


@pytest.mark.asyncio
async def test_deadline_discovery_visits_multiple_pages_without_marker_or_wake(
    database: Database,
) -> None:
    case = await execution_case(database)
    now = case.promotion.clock.now()
    ids: list[str] = []
    async with database.transaction() as writer:
        seed = Seeder(writer)
        for number in range(2, 214):
            ids.append(await seed.attempt(case.node_id, "claimed", number))
        await writer.execute(
            update(attempt)
            .where(attempt.c.node_instance_id == case.node_id)
            .values(deadline=now, lease_until=now)
        )
        await writer.execute(
            update(run)
            .where(run.c.id == case.promotion.run_id)
            .values(status="failed", needs_scheduling=False)
        )
    expected = {case.attempt_id, *ids}
    deadlines, leases = DueAttemptSweep("deadline"), DueAttemptSweep("lease")

    async with database.read_transaction() as reader:
        first = await deadlines.next_page(reader, now)
        lease_first = await leases.next_page(reader, now)
    async with database.transaction() as writer:
        late = await Seeder(writer).attempt(case.node_id, "claimed", 214)
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == late)
            .values(
                deadline=now - timedelta(days=1), lease_until=now - timedelta(days=1)
            )
        )
    async with database.read_transaction() as reader:
        second = await deadlines.next_page(reader, now + timedelta(days=1))
        third = await deadlines.next_page(reader, now + timedelta(days=1))
        lease_second = await leases.next_page(reader, now)
        lease_third = await leases.next_page(reader, now)
        wrapped = await deadlines.next_page(reader, now)

    assert [len(first), len(second), len(third)] == [100, 100, 13]
    assert {identity for identity, _ in first + second + third} == expected
    assert {
        identity for identity, _ in lease_first + lease_second + lease_third
    } == expected
    assert {owner for _, owner in first + second + third} == {case.promotion.run_id}
    assert wrapped[0] == (late, case.promotion.run_id)
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_terminal_run_recovery_remains_discoverable(database: Database) -> None:
    case = await execution_case(database)
    case.promotion.clock.advance(30)
    async with database.transaction() as writer:
        await writer.execute(
            update(run)
            .where(run.c.id == case.promotion.run_id)
            .values(status="cancelled", needs_scheduling=False)
        )
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == case.node_id)
            .values(current_attempt_id=None)
        )

    async with database.read_transaction() as reader:
        found = await DueAttemptSweep("lease").next_page(
            reader, case.promotion.clock.now()
        )
    async with database.transaction() as writer:
        recovered = await recover_expired_lease(
            await case.context(writer, False), found[0][0]
        )
    saved = await case.snapshot(database)

    assert found == ((case.attempt_id, case.promotion.run_id),)
    assert recovered
    assert saved["execution_domain_id"] == case.domain_id
    assert saved["recovery_required"] is True
    assert saved["status"] == "running"


@pytest.mark.asyncio
async def test_running_deadline_discovery_includes_unclaimed_and_wait_only_runs(
    database: Database,
) -> None:
    case = await execution_case(database)
    ids = [case.promotion.run_id]
    async with database.transaction() as writer:
        seed = Seeder(writer)
        for _ in range(211):
            ids.append(
                await seed.run(
                    case.promotion.scope.namespace_id,
                    case.promotion.scope.graph_id,
                    case.promotion.scope.version_id,
                    "running",
                )
            )
        await writer.execute(update(run).values(needs_scheduling=False))
    sweep = RunningRunSweep()

    async with database.read_transaction() as reader:
        pages = [await sweep.next_page(reader) for _ in range(3)]
        ordinary_marker = await reader.scalar(
            select(run.c.id).where(run.c.needs_scheduling.is_(True)).limit(1)
        )

    assert [len(page.run_ids) for page in pages] == [100, 100, 12]
    assert {identity for page in pages for identity in page.run_ids} == set(ids)
    assert ordinary_marker is None
