import asyncio
import sys
from types import FrameType

import pytest
from sqlalchemy import select, update

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database, Dialect
from kora.db.tables import wait
from kora.engine.admission_processing import AdmissionProcessingFailed
from kora.engine.attempt_admission import (
    AttemptAdmissionState,
    PreparedAttemptAdmission,
)
from kora.engine.locks import TransitionLocks
from tests.api.deployment_cases import preparer
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger, until
from tests.db.seed import Seeder
from tests.engine.admission_preparation_support import observe_detached_checks
from tests.engine.admission_processing_support import process, processor
from tests.engine.admission_snapshot_support import snapshot_case
from tests.engine.test_claim_snapshot import PausedClaimRead

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
async def test_admission_processing_stops_future_and_refreshed_periodic_waits(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    owner = processor(database, case)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        await seed.attempt(await seed.node(case.promotion.run_id, "history"), "created")
    assert await process(owner, case.node_id) is AttemptAdmissionState.WAITING
    async with database.read_transaction() as reader:
        wait_id = await reader.scalar(select(wait.c.id))
    assert isinstance(wait_id, str)
    ledger = ConnectionLedger(database)

    with observe_detached_checks(database) as checks:
        with pytest.raises(AdmissionProcessingFailed):
            await process(owner, case.node_id)
    case.promotion.clock.advance(owner.interval.total_seconds())
    with PausedClaimRead(database) as pause:
        pending = asyncio.create_task(process(owner, case.node_id))
        try:
            await asyncio.wait_for(pause.entered.wait(), 10.0)
            async with database.transaction() as writer:
                await writer.execute(
                    update(wait)
                    .where(wait.c.id == wait_id)
                    .values(wake_at=case.promotion.clock.now() + owner.interval)
                )
            before = await case.promotion.snapshot(database)
        finally:
            pause.release.set()
            outcome = await asyncio.wait_for(pending, 10.0)
    after = await case.promotion.snapshot(database)
    async with database.read_transaction() as reader:
        saved = await reader.scalar(select(wait.c.status).where(wait.c.id == wait_id))

    assert checks == []
    assert outcome is AttemptAdmissionState.REFUSED
    assert before == after
    assert saved == "open"
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_admission_processing_releases_contended_original_writer(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    owner = processor(database, case)
    before = await case.promotion.snapshot(database)
    prepared = asyncio.Event()
    ledger = ConnectionLedger(database)
    previous = sys.getprofile()

    def observe(frame: FrameType, event: str, argument: object) -> None:
        if (
            event == "return"
            and frame.f_code is PreparedAttemptAdmission.__init__.__code__
        ):
            prepared.set()

    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(case.promotion.run_id)
        sys.setprofile(observe)
        pending = asyncio.create_task(process(owner, case.node_id))
        try:
            await asyncio.wait_for(prepared.wait(), 10.0)
            if database.dialect is Dialect.SQLITE:
                await until(lambda: database.units_in_flight() == 2)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(pending, 10.0)
            else:
                assert (
                    await asyncio.wait_for(pending, 10.0)
                    is AttemptAdmissionState.REFUSED
                )
        finally:
            sys.setprofile(previous)
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    after = await case.promotion.snapshot(database)

    assert before == after
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
