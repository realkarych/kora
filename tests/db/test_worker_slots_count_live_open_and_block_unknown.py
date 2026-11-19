from datetime import timedelta

import pytest
from sqlalchemy import select, update

from kora.db.connection import Database
from kora.db.tables import attempt
from kora.db.worker_claims import (
    unknown_worker_slot,
    worker_slot_available,
)
from tests.db.seed import START, Seeder
from tests.engine.test_grant_history import seed


@pytest.mark.asyncio
async def test_worker_slots_count_live_open_and_block_unknown(
    database: Database,
) -> None:
    scope = await seed(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        second = await seeder.attempt(scope.node_id, number=2)
        third = await seeder.attempt(scope.node_id, number=3)
        await writer.execute(
            update(attempt).values(
                worker_id="worker-1",
                lease_until=START + timedelta(seconds=120),
                current_dispatcher_open=True,
            )
        )

    async with database.transaction() as writer:
        full = await worker_slot_available(writer, "worker-1", 2, START)
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == second)
            .values(
                current_dispatcher_open=False,
            )
        )
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == third)
            .values(
                lease_until=START,
            )
        )
        room = await worker_slot_available(writer, "worker-1", 2, START)
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == scope.attempt_id)
            .values(
                current_dispatcher_open=None,
            )
        )
        unknown = await unknown_worker_slot(writer, "worker-1", START)
        blocked = await worker_slot_available(writer, "worker-1", 2, START)
        expired = await worker_slot_available(
            writer, "worker-1", 2, START + timedelta(seconds=120)
        )

    assert (full, room, blocked, expired) == (False, True, False, True)
    assert unknown == scope.attempt_id
    async with database.read_transaction() as reader:
        assert (
            list(await reader.scalars(select(attempt.c.execution_state)))
            == ["unknown"] * 3
        )
