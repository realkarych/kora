import pytest
from sqlalchemy import select, update

from kora.db.connection import Database
from kora.db.tables import attempt
from kora.engine.admission_discovery import AdmissionSweep
from kora.engine.attempt_admission import AttemptAdmissionState, admit_attempt
from kora.engine.attempt_capacity import PreparedGraphCaps
from tests.db.seed import Seeder
from tests.engine.admission_wait_support import INTERVAL, LIMITS
from tests.engine.attempt_admission_support import (
    append_admission_case,
    attempt_admission_case,
)
from tests.engine.expiry_support import expiry_context
from tests.engine.optional_input_support import optional_admission_case


@pytest.mark.asyncio
@pytest.mark.parametrize("block_oldest", [False, True])
async def test_discovered_order_admits_oldest_eligible_without_blocking_peer(
    database: Database, block_oldest: bool
) -> None:
    first = await attempt_admission_case(database)
    second = await append_admission_case(database, first)
    cases = {case.binding.node_id: case for case in (first, second)}
    if block_oldest:
        async with database.transaction() as writer:
            seed = Seeder(writer)
            blocker = await seed.attempt(
                await seed.node(first.promotion.run_id, "history", "cancelled"),
                "cancelled",
            )
            await writer.execute(
                update(attempt)
                .where(attempt.c.id == blocker)
                .values(recovery_required=True)
            )
    async with database.read_transaction() as reader:
        page = await AdmissionSweep().next_page(reader)
    outcomes: dict[str, AttemptAdmissionState] = {}

    for identity in page.node_ids:
        if identity not in cases:
            continue
        case = cases[identity]
        prepared = case.prepare()
        async with database.transaction() as writer:
            outcomes[identity] = await admit_attempt(
                await expiry_context(writer, case.promotion),
                prepared,
                LIMITS,
                PreparedGraphCaps(case.promotion.scope, None, None),
                global_limit=1,
                runtime_limit=1,
                interval=INTERVAL,
                periodic_recheck=True,
            )
    async with database.read_transaction() as reader:
        with await reader.execute(
            select(attempt.c.id).where(attempt.c.status == "created")
        ) as result:
            admitted = result.scalars().all()

    expected = second if block_oldest else first
    assert admitted == [expected.binding.attempt_id]
    assert outcomes[expected.binding.node_id] is AttemptAdmissionState.QUEUED
    other = first if block_oldest else second
    assert outcomes[other.binding.node_id] is AttemptAdmissionState.WAITING


@pytest.mark.asyncio
async def test_discovery_does_not_authorize_unfinished_control_dependency(
    database: Database,
) -> None:
    case = await optional_admission_case(
        database, state="running", inputless_command=True
    )
    async with database.read_transaction() as reader:
        page = await AdmissionSweep().next_page(reader)
    prepared = case.prepare()

    async with database.transaction() as writer:
        outcome = await admit_attempt(
            await expiry_context(writer, case.promotion),
            prepared,
            LIMITS,
            PreparedGraphCaps(case.promotion.scope, None, None),
            global_limit=1,
            runtime_limit=1,
            interval=INTERVAL,
            periodic_recheck=True,
        )

    assert case.binding.node_id in page.node_ids
    assert outcome is AttemptAdmissionState.REFUSED
    async with database.read_transaction() as reader:
        assert await reader.scalar(select(attempt.c.id)) is None
