from dataclasses import replace

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.engine.admission_processing import AdmissionProcessingFailed
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.engine.attempt_preparation_budget import (
    AttemptPreparationLimits,
    attempt_preparation_reservation,
)
from tests.api.deployment_cases import preparer
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger
from tests.engine.admission_preparation_support import observe_detached_checks, prepare
from tests.engine.admission_processing_support import process, processor
from tests.engine.admission_snapshot_support import snapshot_case

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["retained", "work"])
@pytest.mark.parametrize("short", [False, True])
async def test_admission_processing_admits_fresh_parameter_and_claim_work(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    field: str,
    short: bool,
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    owner = processor(database, case)
    source = await prepare(database, case)
    budget = AttemptPreparationLimits(
        owner.limits.preparation.snapshot.graph,
        source.reservation.retained_units,
        owner.limits.preparation.retained_units,
        owner.limits.claim_work,
    )
    needed = attempt_preparation_reservation(
        source.checked, budget, reconstruct_params=True
    )
    owner = replace(
        owner,
        limits=replace(
            owner.limits,
            preparation=replace(
                owner.limits.preparation,
                retained_units=needed.retained_units
                - int(short and field == "retained"),
            ),
            claim_work=needed.work_units - int(short and field == "work"),
        ),
    )
    del source
    before = await case.promotion.snapshot(database)
    ledger = ConnectionLedger(database)

    with observe_detached_checks(database) as checks:
        if short:
            with pytest.raises(AdmissionProcessingFailed):
                await process(owner, case.node_id)
        else:
            assert await process(owner, case.node_id) is AttemptAdmissionState.QUEUED
    checkouts = ledger.checkouts
    after = await case.promotion.snapshot(database)

    assert checkouts == (1 if short else 2)
    assert checks.count("decode_run_input_value") == (1 if short else 2)
    assert (before == after) is short
    assert ledger.outstanding == database.units_in_flight() == 0
