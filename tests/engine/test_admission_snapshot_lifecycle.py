import asyncio
from typing import cast

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database, Lifecycle
from kora.engine.admission_snapshot import (
    AdmissionSnapshotRejected,
    acquire_admission_snapshot,
)
from kora.engine.namespace_policy import NamespacePolicyConfiguration
from kora.engine.price_initialization import PriceConfiguration
from kora.ir.json import JsonObject
from tests.api.deployment_cases import preparer, skeleton_ir
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger, narrow_database, until
from tests.db.seed import START
from tests.engine.admission_snapshot_support import LIMITS, acquire, snapshot_case
from tests.engine.test_admission_initialization import initializer, rates
from tests.engine.test_claim_snapshot import PausedClaimRead

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
async def test_admission_snapshot_is_coherent(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    changed_ir = skeleton_ir()
    cast(JsonObject, changed_ir["runtime"])["concurrency"] = {"max_running_nodes": 2}
    namespace_id = case.promotion.scope.namespace_id
    replacement = initializer(
        database,
        (
            NamespacePolicyConfiguration.prepare(
                namespace_id, 2, {"max_pending_runs": 500}
            ),
        ),
        (
            PriceConfiguration(
                namespace_id, case.promotion.clock.now(), {"model": rates("2")}
            ),
        ),
    )

    with PausedClaimRead(database) as pause:
        pending = asyncio.create_task(acquire(database, case))
        await asyncio.wait_for(pause.entered.wait(), 10.0)
        try:
            deployed = await case.publish(changed_ir)
            await replacement.initialize(api_capture.diagnostics)
        finally:
            pause.release.set()
        old = await pending
    new = await acquire(database, case)

    assert old.policy.revision == 1
    assert new.policy.revision == 2
    assert old.price_table_id == case.price_id
    assert new.price_table_id == replacement.prices[0].id
    assert old.cap_source.version_id == case.promotion.scope.version_id
    assert new.cap_source.version_id == deployed.graph_version_id.removeprefix("gv_")
    assert old.candidate == new.candidate
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_admission_snapshot_cancellation_releases_its_reader(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    ledger = ConnectionLedger(database)
    with PausedClaimRead(database) as pause:
        pending = asyncio.create_task(acquire(database, case))
        await asyncio.wait_for(pause.entered.wait(), 10.0)

        pending.cancel()
        pause.release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending

    assert ledger.outstanding == database.units_in_flight() == 0
    assert (await acquire(database, case)).candidate.node_id == case.node_id


@pytest.mark.asyncio
async def test_admission_snapshot_expired_deadline_never_checks_out(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)

    with pytest.raises(AdmissionSnapshotRejected):
        await acquire_admission_snapshot(
            database,
            "01J00000000000000000000000",
            LIMITS,
            observed_at=START,
            periodic_recheck=True,
            deadline=asyncio.get_running_loop().time() - 1.0,
        )

    assert ledger.checkouts == ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_admission_snapshot_deadline_cancels_held_acquisition(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    ledger = ConnectionLedger(database)
    deadline = asyncio.get_running_loop().time() + 1.0
    with PausedClaimRead(database) as pause:
        pending = asyncio.create_task(
            acquire_admission_snapshot(
                database,
                case.node_id,
                LIMITS,
                observed_at=case.promotion.clock.now(),
                periodic_recheck=True,
                deadline=deadline,
            )
        )
        await asyncio.wait_for(pause.entered.wait(), 10.0)

        try:
            with pytest.raises(AdmissionSnapshotRejected):
                await pending
        finally:
            pause.release.set()

    assert ledger.outstanding == database.units_in_flight() == 0
    assert (await acquire(database, case)).candidate.node_id == case.node_id


@pytest.mark.asyncio
async def test_admission_snapshot_settles_during_graceful_shutdown(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    reader = narrow_database(database, pool_size=1)
    ledger = ConnectionLedger(reader)
    with PausedClaimRead(reader) as pause:
        pending = asyncio.create_task(acquire(reader, case))
        await asyncio.wait_for(pause.entered.wait(), 10.0)
        closing = asyncio.create_task(reader.close(10.0))

        try:
            await until(lambda: reader.lifecycle is Lifecycle.CLOSING)
        finally:
            pause.release.set()
        snapshot = await pending
        await closing

    assert snapshot.candidate.node_id == case.node_id
    assert reader.lifecycle is Lifecycle.CLOSED
    assert ledger.opened == ledger.closed == 1
    assert ledger.outstanding == reader.units_in_flight() == 0
