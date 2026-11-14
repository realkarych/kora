from datetime import timedelta

import pytest
from sqlalchemy import select, update

from kora.db.admission_waits import due_admission_wait
from kora.db.connection import Database
from kora.db.tables import attempt, wait
from kora.engine.attempt_admission import AttemptAdmissionState, admit_attempt
from kora.engine.attempt_capacity import PreparedGraphCaps
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.admission_wait_support import INTERVAL, LIMITS
from tests.engine.attempt_admission_support import attempt_admission_case
from tests.engine.expiry_support import expiry_context


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["before", "equal", "after", "refreshed"])
async def test_periodic_admission_rechecks_current_due_wait(
    database: Database, timing: str
) -> None:
    case = await attempt_admission_case(database)
    prepared = case.prepare()
    caps = PreparedGraphCaps(case.promotion.scope, None, None)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        await seed.attempt(await seed.node(case.promotion.run_id, "history"), "created")
        assert (
            await admit_attempt(
                await expiry_context(writer, case.promotion),
                prepared,
                LIMITS,
                caps,
                global_limit=1,
                runtime_limit=None,
                interval=INTERVAL,
            )
            is AttemptAdmissionState.WAITING
        )
    async with database.read_transaction() as reader:
        wait_id = await reader.scalar(select(wait.c.id))
    assert isinstance(wait_id, str)
    advance = (
        INTERVAL.total_seconds()
        + {"before": -1, "equal": 0, "after": 1, "refreshed": 0}[timing]
    )
    case.promotion.clock.advance(advance)
    if timing == "refreshed":
        async with database.read_transaction() as reader:
            assert (
                await due_admission_wait(
                    reader, case.binding.node_id, case.promotion.clock.now()
                )
                == wait_id
            )
        async with database.transaction() as writer:
            await expiry_context(writer, case.promotion)
            await writer.execute(
                update(wait)
                .where(wait.c.id == wait_id)
                .values(wake_at=case.promotion.clock.now() + INTERVAL)
            )
    before = await case.promotion.snapshot(database)
    ledger = ConnectionLedger(database)

    async with database.transaction() as writer:
        outcome = await admit_attempt(
            await expiry_context(writer, case.promotion),
            prepared,
            LIMITS,
            caps,
            global_limit=2,
            runtime_limit=None,
            interval=INTERVAL,
            expected_wait_id=wait_id,
            periodic_recheck=True,
        )
    async with database.read_transaction() as reader:
        status = await reader.scalar(select(wait.c.status).where(wait.c.id == wait_id))
        saved = await reader.scalar(
            select(attempt.c.id).where(attempt.c.id == case.binding.attempt_id)
        )

    refused = timing in ("before", "refreshed")
    assert outcome is (
        AttemptAdmissionState.REFUSED if refused else AttemptAdmissionState.QUEUED
    )
    assert status == ("open" if refused else "satisfied")
    assert saved == (None if refused else case.binding.attempt_id)
    if refused:
        assert await case.promotion.snapshot(database) == before
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_recovery_change_can_recheck_before_periodic_wake(
    database: Database,
) -> None:
    case = await attempt_admission_case(database)
    prepared = case.prepare()
    caps = PreparedGraphCaps(case.promotion.scope, None, None)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        blocker = await seed.attempt(
            await seed.node(case.promotion.run_id, "history", "cancelled"), "finishing"
        )
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == blocker)
            .values(recovery_required=True)
        )
        assert (
            await admit_attempt(
                await expiry_context(writer, case.promotion),
                prepared,
                LIMITS,
                caps,
                global_limit=1,
                runtime_limit=1,
                interval=INTERVAL,
            )
            is AttemptAdmissionState.WAITING
        )
    async with database.read_transaction() as reader:
        with await reader.execute(select(wait)) as rows:
            saved = dict(rows.mappings().one())
    async with database.transaction() as writer:
        await expiry_context(writer, case.promotion)
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == blocker)
            .values(
                recovery_required=False, status="cancelled", execution_state="stopped"
            )
        )
    assert saved["wake_at"] == case.promotion.clock.now() + INTERVAL

    async with database.transaction() as writer:
        periodic = await admit_attempt(
            await expiry_context(writer, case.promotion),
            prepared,
            LIMITS,
            caps,
            global_limit=1,
            runtime_limit=1,
            interval=INTERVAL,
            expected_wait_id=str(saved["id"]),
            periodic_recheck=True,
        )
    async with database.transaction() as writer:
        changed = await admit_attempt(
            await expiry_context(writer, case.promotion),
            prepared,
            LIMITS,
            caps,
            global_limit=1,
            runtime_limit=1,
            interval=INTERVAL,
            expected_wait_id=str(saved["id"]),
        )

    assert periodic is AttemptAdmissionState.REFUSED
    assert changed is AttemptAdmissionState.QUEUED
    assert saved["reason"] == {
        "version": 1,
        "kind": "execution_recovery",
        "observed_at": case.promotion.clock.now().isoformat(timespec="microseconds"),
        "blockers": {"count": 1, "ids": [blocker], "truncated": False},
    }
    assert INTERVAL > timedelta(0)
    assert database.units_in_flight() == 0
