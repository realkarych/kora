import asyncio
from typing import cast

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.engine.admission_processing import AdmissionProcessingFailed
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.engine.namespace_policy import NamespacePolicyConfiguration
from kora.engine.price_initialization import PriceConfiguration
from kora.ir.json import JsonObject
from tests.api.deployment_cases import preparer, skeleton_ir
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger
from tests.engine.admission_processing_support import process, processor
from tests.engine.admission_snapshot_support import snapshot_case
from tests.engine.test_admission_initialization import initializer, rates
from tests.engine.test_claim_snapshot import PausedClaimRead

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["policy", "price", "caps"])
async def test_admission_processing_rechecks_current_sources_before_writing(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    source: str,
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    owner = processor(database, case)
    before = await case.promotion.snapshot(database)
    ledger = ConnectionLedger(database)

    with PausedClaimRead(database) as pause:
        pending = asyncio.create_task(process(owner, case.node_id))
        try:
            await asyncio.wait_for(pause.entered.wait(), 10.0)
            if source == "caps":
                ir = skeleton_ir()
                cast(JsonObject, ir["runtime"])["concurrency"] = {
                    "max_running_nodes": 2
                }
                await case.publish(ir)
            else:
                namespace_id = case.promotion.scope.namespace_id
                configuration = initializer(
                    database,
                    (
                        NamespacePolicyConfiguration.prepare(
                            namespace_id, 2, {"max_pending_runs": 1000}
                        ),
                    )
                    if source == "policy"
                    else (),
                    (
                        PriceConfiguration(
                            namespace_id,
                            case.promotion.clock.now(),
                            {"model": rates("2")},
                        ),
                    )
                    if source == "price"
                    else (),
                )
                await configuration.initialize(api_capture.diagnostics)
        finally:
            pause.release.set()
            if source == "caps":
                with pytest.raises(AdmissionProcessingFailed) as failure:
                    await asyncio.wait_for(pending, 10.0)
                assert failure.value.__cause__ is failure.value.__context__ is None
                outcome = None
            else:
                outcome = await asyncio.wait_for(pending, 10.0)
    after = await case.promotion.snapshot(database)

    assert outcome is (None if source == "caps" else AttemptAdmissionState.REFUSED)
    assert before == after
    assert ledger.outstanding == database.units_in_flight() == 0
