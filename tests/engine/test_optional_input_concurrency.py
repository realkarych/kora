import asyncio

import pytest
from sqlalchemy import select

from kora.db.claim_sources import acquire_claim_sources
from kora.db.connection import Database
from kora.db.tables import attempt
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.engine.claim_sources import decode_claim_sources
from tests.db.closing import ConnectionLedger
from tests.engine.admission_wait_support import LIMITS
from tests.engine.lock_support import (
    DEADLINE,
    DeliberateRollback,
    lock_inventory,
    wait_for_lock_waiter,
)
from tests.engine.optional_input_support import (
    admit_optional_candidate,
    optional_admission_case,
    set_optional_producer_state,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("current", ["pending", "succeeded", "skipped", "failed"])
async def test_optional_admission_uses_current_facts_after_committed_clear(
    database: Database, current: str
) -> None:
    case = await optional_admission_case(database, "skipped")
    prepared = case.prepare()
    ledger = ConnectionLedger(database)
    follower: asyncio.Task[AttemptAdmissionState] | None = None

    async def follow() -> AttemptAdmissionState:
        async with database.transaction() as writer:
            return await admit_optional_candidate(writer, case, prepared)

    async with database.transaction() as writer:
        await set_optional_producer_state(writer, case, "pending")
    try:
        async with database.transaction() as writer:
            await set_optional_producer_state(writer, case, current)
            follower = asyncio.create_task(follow())
            await wait_for_lock_waiter(database)
        result = await asyncio.wait_for(follower, DEADLINE)
    finally:
        if follower is not None and not follower.done():
            follower.cancel()
            with pytest.raises(asyncio.CancelledError):
                await follower
    async with database.read_transaction() as reader:
        sources = await acquire_claim_sources(reader, case.binding, LIMITS)
        with await reader.execute(select(attempt.c.id)) as rows:
            attempts = rows.scalars().all()

    if current in ("skipped", "failed"):
        assert result is AttemptAdmissionState.QUEUED
        assert sources is not None
        assert attempts == [case.binding.attempt_id]
    else:
        assert result is AttemptAdmissionState.REFUSED
        assert sources is None
        assert attempts == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert await lock_inventory(database) == []
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_optional_binding_history_survives_later_producer_clear(
    database: Database,
) -> None:
    case = await optional_admission_case(database, "skipped")
    prepared = case.prepare()
    original_contract = case.checked.canonical_contract
    original_pin = case.checked.pin
    ledger = ConnectionLedger(database)
    follower: asyncio.Task[None] | None = None

    async def clear_producer() -> None:
        async with database.transaction() as writer:
            await set_optional_producer_state(writer, case, "pending")

    try:
        async with database.transaction() as writer:
            admitted = await admit_optional_candidate(writer, case, prepared)
            before = await acquire_claim_sources(writer, case.binding, LIMITS)
            follower = asyncio.create_task(clear_producer())
            await wait_for_lock_waiter(database)
        await asyncio.wait_for(follower, DEADLINE)
    finally:
        if follower is not None and not follower.done():
            follower.cancel()
            with pytest.raises(asyncio.CancelledError):
                await follower
    async with database.transaction() as writer:
        replay = await admit_optional_candidate(writer, case, prepared)
    async with database.read_transaction() as reader:
        after = await acquire_claim_sources(reader, case.binding, LIMITS)
        with await reader.execute(select(attempt.c.id)) as rows:
            attempts = rows.scalars().all()

    assert admitted is AttemptAdmissionState.QUEUED
    assert replay is AttemptAdmissionState.REFUSED
    assert before is not None
    assert after is not None
    assert decode_claim_sources(after) == decode_claim_sources(before)
    assert attempts == [case.binding.attempt_id]
    assert case.checked.canonical_contract == original_contract
    assert case.checked.pin == original_pin
    assert ledger.outstanding == database.units_in_flight() == 0
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["rollback", "cancel"])
async def test_optional_admission_failure_releases_attempt_and_sources(
    database: Database, ending: str
) -> None:
    case = await optional_admission_case(database)
    prepared = case.prepare()
    before = await case.promotion.snapshot(database)
    applied, release = asyncio.Event(), asyncio.Event()
    ledger = ConnectionLedger(database)

    async def hold() -> None:
        async with database.transaction() as writer:
            result = await admit_optional_candidate(writer, case, prepared)
            assert result is AttemptAdmissionState.QUEUED
            applied.set()
            await asyncio.wait_for(release.wait(), DEADLINE)
            raise DeliberateRollback()

    task = asyncio.create_task(hold())
    try:
        await asyncio.wait_for(applied.wait(), DEADLINE)
    finally:
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, DEADLINE)
        else:
            release.set()
            with pytest.raises(DeliberateRollback):
                await asyncio.wait_for(task, DEADLINE)
    async with database.read_transaction() as reader:
        sources = await acquire_claim_sources(reader, case.binding, LIMITS)
        saved_id = await reader.scalar(select(attempt.c.id))

    assert sources is saved_id is None
    assert await case.promotion.snapshot(database) == before
    assert ledger.outstanding == database.units_in_flight() == 0
    assert await lock_inventory(database) == []
    assert database.writer_is_free()
