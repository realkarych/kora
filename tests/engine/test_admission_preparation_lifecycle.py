import asyncio
import sys
from datetime import datetime
from types import FrameType

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database, DatabaseSettings
from kora.engine.admission_preparation import (
    AdmissionPreparationRejected,
    PreparedAdmissionSnapshot,
    prepare_admission_snapshot,
)
from tests.api.deployment_cases import preparer
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger, WriterCutoffLoop
from tests.engine.admission_preparation_support import LIMITS, prepare
from tests.engine.admission_snapshot_support import snapshot_case
from tests.engine.test_claim_snapshot import PausedClaimRead

__all__ = ["api_capture", "preparer"]


def refuse_detached_handoff(
    settings: DatabaseSettings, node_id: str, observed_at: datetime, cancel: bool
) -> None:
    loop = WriterCutoffLoop()

    async def exercise() -> None:
        database = Database(settings)
        ledger = ConnectionLedger(database)
        deadline = loop.time() + 10.0
        observed = False
        previous = sys.getprofile()

        def observe(frame: FrameType, event: str, argument: object) -> None:
            nonlocal observed
            if (
                event != "return"
                or frame.f_code is not PreparedAdmissionSnapshot.__init__.__code__
            ):
                return
            assert not observed
            assert database.units_in_flight() == ledger.outstanding == 0
            observed = True
            if cancel:
                current = asyncio.current_task()
                assert current is not None
                current.cancel()
            else:
                loop.advance_to(deadline)

        try:
            sys.setprofile(observe)
            with pytest.raises(
                asyncio.CancelledError if cancel else AdmissionPreparationRejected
            ) as failure:
                await prepare_admission_snapshot(
                    database,
                    node_id,
                    LIMITS,
                    observed_at=observed_at,
                    periodic_recheck=True,
                    deadline=deadline,
                )
        finally:
            sys.setprofile(previous)
            await database.close()

        assert observed
        assert failure.value.__cause__ is failure.value.__context__ is None
        assert ledger.checkouts == ledger.opened == ledger.closed == 1
        assert ledger.outstanding == database.units_in_flight() == 0

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        runner.run(exercise())


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_admission_preparation_refuses_expired_or_cancelled_detached_handoff(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture, cancel: bool
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    before = await case.promotion.snapshot(database)

    await asyncio.to_thread(
        refuse_detached_handoff,
        database.settings,
        case.node_id,
        case.promotion.clock.now(),
        cancel,
    )
    after = await case.promotion.snapshot(database)

    assert before == after
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_admission_preparation_cancels_acquisition_and_releases_reader(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    ledger = ConnectionLedger(database)
    with PausedClaimRead(database) as pause:
        pending = asyncio.create_task(prepare(database, case))
        await asyncio.wait_for(pause.entered.wait(), 10.0)

        pending.cancel()
        pause.release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending

    assert ledger.outstanding == database.units_in_flight() == 0
    assert (await prepare(database, case)).checked.pin == case.promotion.pin
