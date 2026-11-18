import asyncio
from typing import Literal

import pytest
from sqlalchemy import select

from kora.db.claim_sources import acquire_claim_sources
from kora.db.connection import Database
from kora.db.tables import attempt, wait
from kora.engine.attempt_admission import AttemptAdmissionState, admit_attempt
from kora.engine.attempt_capacity import PreparedGraphCaps
from tests.db.closing import ConnectionLedger
from tests.engine.admission_wait_support import INTERVAL, LIMITS
from tests.engine.attempt_admission_support import (
    append_admission_case,
    attempt_admission_case,
)
from tests.engine.expiry_support import expiry_context
from tests.engine.lock_support import (
    DEADLINE,
    DeliberateRollback,
    lock_inventory,
    wait_for_lock_waiter,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["commit", "rollback", "cancel"])
async def test_attempt_admission_serializes_last_slot_and_releases_failed_reservation(
    database: Database, ending: Literal["commit", "rollback", "cancel"]
) -> None:
    first = await attempt_admission_case(database)
    second = await append_admission_case(database, first)
    first_prepared, second_prepared = first.prepare(), second.prepare()
    caps = PreparedGraphCaps(first.promotion.scope, None, None)
    before = await first.promotion.snapshot(database)
    entered, release = asyncio.Event(), asyncio.Event()
    ledger = ConnectionLedger(database)

    async def hold() -> None:
        async with database.transaction() as writer:
            result = await admit_attempt(
                await expiry_context(writer, first.promotion),
                first_prepared,
                LIMITS,
                caps,
                global_limit=1,
                runtime_limit=1,
                interval=INTERVAL,
            )
            assert result is AttemptAdmissionState.QUEUED
            entered.set()
            await asyncio.wait_for(release.wait(), DEADLINE)
            if ending == "rollback":
                raise DeliberateRollback()

    async def follow() -> AttemptAdmissionState:
        async with database.transaction() as writer:
            return await admit_attempt(
                await expiry_context(writer, second.promotion),
                second_prepared,
                LIMITS,
                caps,
                global_limit=1,
                runtime_limit=1,
                interval=INTERVAL,
            )

    holder = asyncio.create_task(hold())
    follower: asyncio.Task[AttemptAdmissionState] | None = None
    observed: AttemptAdmissionState | None = None
    try:
        await asyncio.wait_for(entered.wait(), DEADLINE)
        follower = asyncio.create_task(follow())
        await wait_for_lock_waiter(database)
    finally:
        if ending == "cancel":
            holder.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(holder, DEADLINE)
        else:
            release.set()
            if ending == "rollback":
                with pytest.raises(DeliberateRollback):
                    await asyncio.wait_for(holder, DEADLINE)
            else:
                await asyncio.wait_for(holder, DEADLINE)
        if follower is not None:
            observed = await asyncio.wait_for(follower, DEADLINE)
    async with database.read_transaction() as reader:
        with await reader.execute(select(attempt.c.id)) as rows:
            attempts = rows.scalars().all()
        with await reader.execute(select(wait)) as rows:
            waits = rows.mappings().all()
        first_sources = await acquire_claim_sources(reader, first.binding, LIMITS)
        second_sources = await acquire_claim_sources(reader, second.binding, LIMITS)
    after = await first.promotion.snapshot(database)

    assert follower is not None
    assert attempts == [
        first.binding.attempt_id if ending == "commit" else second.binding.attempt_id
    ]
    if ending == "commit":
        assert observed is AttemptAdmissionState.WAITING
        assert first_sources is not None
        assert second_sources is None
        assert len(waits) == 1
        assert waits[0]["node_instance_id"] == second.binding.node_id
        assert waits[0]["attempt_id"] is None
        assert waits[0]["status"] == "open"
    else:
        assert observed is AttemptAdmissionState.QUEUED
        assert first_sources is None
        assert second_sources is not None
        assert waits == []
        assert after == before
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
