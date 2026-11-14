from datetime import timedelta

import pytest
from sqlalchemy import insert, update

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import (
    node_instance,
    wait,
)
from kora.engine.admission_snapshot import AdmissionSnapshotRejected
from tests.api.deployment_cases import preparer
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger, narrow_database
from tests.db.seed import Seeder
from tests.engine.admission_snapshot_support import (
    acquire,
    snapshot_case,
)
from tests.engine.test_claim_snapshot import ClaimQueries

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-1, 0, 1])
@pytest.mark.parametrize("periodic", [False, True])
async def test_admission_snapshot_wait_gate_precedes_graph_reads(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    offset: int,
    periodic: bool,
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        wait_id = seed.new_id()
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == case.node_id)
            .values(status="waiting")
        )
        await writer.execute(
            insert(wait).values(
                id=wait_id,
                node_instance_id=case.node_id,
                kind="admission",
                status="open",
                wake_at=case.promotion.clock.now() + timedelta(seconds=offset),
                **seed.stamps(),
            )
        )
    reader = narrow_database(database, pool_size=1)
    ledger = ConnectionLedger(reader)

    try:
        with ClaimQueries(reader) as queries:
            if periodic and offset > 0:
                with pytest.raises(AdmissionSnapshotRejected):
                    await acquire(reader, case, periodic_recheck=periodic)
            else:
                acquired = await acquire(reader, case, periodic_recheck=periodic)
                assert acquired.wait_id == wait_id
    finally:
        await reader.close()

    if periodic and offset > 0:
        assert all(
            "graph_version_read_admission" not in sql for sql in queries.statements
        )
    assert ledger.outstanding == reader.units_in_flight() == 0
