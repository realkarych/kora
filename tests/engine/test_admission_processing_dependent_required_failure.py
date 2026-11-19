from typing import cast

import pytest
from sqlalchemy import select, update

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import attempt, node_instance
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.ir.json import JsonObject
from tests.api.deployment_cases import preparer, skeleton_ir
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger
from tests.engine.admission_processing_support import process, processor
from tests.engine.admission_snapshot_support import snapshot_case

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("optional", "producer_state"),
    [(False, "failed")],
)
async def test_admission_processing_uses_common_dependent_authority(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    optional: bool,
    producer_state: str,
) -> None:
    ir = skeleton_ir()
    nodes = cast(JsonObject, cast(JsonObject, ir["graph"])["nodes"])
    gate = cast(JsonObject, nodes["gate"])
    gate["trigger_rule"] = "all_done"
    if optional:
        gate["inputs"] = {
            "session": {
                "from": "agent",
                "output": "session",
                "type": "AgentSession",
                "optional": True,
            }
        }
    case = await snapshot_case(database, preparer, api_capture, ir=ir, node_path="gate")
    async with database.transaction() as writer:
        await writer.execute(
            update(node_instance)
            .where(
                node_instance.c.run_id == case.promotion.run_id,
                node_instance.c.node_path == "agent",
            )
            .values(status=producer_state)
        )
    owner = processor(database, case)
    before = await case.promotion.snapshot(database)
    ledger = ConnectionLedger(database)

    outcome = await process(owner, case.node_id)
    after = await case.promotion.snapshot(database)
    async with database.read_transaction() as reader:
        saved = await reader.scalar(select(attempt.c.id))

    refused = producer_state == "succeeded"
    assert outcome is (
        AttemptAdmissionState.REFUSED if refused else AttemptAdmissionState.QUEUED
    )
    assert (saved is None) is refused
    assert (before == after) is refused
    assert next(node for node in after[1] if node["node_path"] == "agent") == next(
        node for node in before[1] if node["node_path"] == "agent"
    )
    assert ledger.outstanding == database.units_in_flight() == 0
