from dataclasses import replace
from typing import cast

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.engine.admission_snapshot import AdmissionSnapshotRejected
from kora.ir.json import JsonObject
from tests.api.deployment_cases import preparer, skeleton_ir
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger
from tests.engine.admission_snapshot_support import (
    LIMITS,
    acquire,
    snapshot_case,
)

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["native", "raw"])
@pytest.mark.parametrize("distinct", [False, True])
async def test_admission_snapshot_reserves_two_graphs_before_checkout(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    kind: str,
    distinct: bool,
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    if distinct:
        ir = skeleton_ir()
        cast(JsonObject, ir["runtime"])["concurrency"] = {"max_running_nodes": 2}
        await case.publish(ir)
    native, raw = LIMITS.reservation()
    exact = replace(LIMITS, native_bytes=native, raw_bytes=raw)
    insufficient = replace(
        exact, **{f"{kind}_bytes": native - 1 if kind == "native" else raw - 1}
    )
    ledger = ConnectionLedger(database)

    with pytest.raises(AdmissionSnapshotRejected):
        await acquire(database, case, limits=insufficient)
    refused_checkouts = ledger.checkouts
    accepted = await acquire(database, case, limits=exact)

    assert refused_checkouts == 0
    assert (accepted.graph is not accepted.cap_graph) is distinct
    assert ledger.checkouts == 1
    assert ledger.outstanding == database.units_in_flight() == 0
