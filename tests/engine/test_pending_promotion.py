import asyncio

import httpx
import pytest
from sqlalchemy import delete, select, update

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.api.installed_worker import installed_worker_profile
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import (
    event,
    inbound_event,
    node_instance,
    run,
    run_promotion_admission,
)
from kora.engine.graph_snapshot import GraphSnapshotLimits
from kora.engine.pending_promotion import PendingPromotion
from kora.engine.run_promotion import PromotionOutcome
from kora.models.graphs import DeployResponse
from kora.models.runs import AcceptedRun
from tests.api.deployment_cases import GRAPH, HARNESS, NAMESPACE, preparer, skeleton_ir
from tests.api.support import api_capture, application, client_for, deployment_services
from tests.db.closing import ConnectionLedger
from tests.db.seed import FakeClock, fixed_randomness
from tests.engine.graph_output_cases import child
from tests.engine.test_http_trigger_admission import KEY, RUNS, VERSIONS
from tests.plugins.authentication.support import seed_case

__all__ = ["api_capture", "preparer"]


def pending_promotion(database: Database) -> PendingPromotion:
    profile = installed_worker_profile("2000")
    assert profile is not None
    snapshot = profile.limits.snapshot
    clock = FakeClock()
    return PendingPromotion(
        database,
        GraphSnapshotLimits(
            snapshot.graph, snapshot.graph_native_bytes, snapshot.owner_text_bytes
        ),
        profile.limits.expiry,
        clock.now,
        IdentifierGenerator(clock.now, fixed_randomness(41)),
    )


async def deploy(
    client: httpx.AsyncClient, headers: dict[str, str], timeout: str
) -> DeployResponse:
    ir = skeleton_ir()
    child(ir, "runtime")["limits"] = {"timeout": timeout}
    response = await client.post(
        VERSIONS, json={"graph": GRAPH, "ir": ir, "resources": []}, headers=headers
    )
    assert response.status_code == 201
    return DeployResponse.model_validate(response.json())


async def trigger(client: httpx.AsyncClient, headers: dict[str, str]) -> AcceptedRun:
    response = await client.post(
        RUNS, json={"params": {}}, headers=headers | {"Idempotency-Key": KEY}
    )
    assert response.status_code == 201
    return AcceptedRun.model_validate(response.json())


@pytest.mark.asyncio
async def test_scheduler_promotes_original_pin_after_later_deploy(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    user = await seed_case(database, NAMESPACE)
    headers = {"Authorization": str(user.request.authorization)}
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    promotion = pending_promotion(database)
    ledger = ConnectionLedger(database)
    async with client_for(app) as client:
        original = await deploy(client, headers, "01h")
        accepted = await trigger(client, headers)
        newer = await deploy(client, headers, "02h")
    run_id = accepted.run_id.removeprefix("run_")
    deadline = asyncio.get_running_loop().time() + 10.0

    outcome = await promotion.process(run_id, deadline=deadline, stopped=lambda: False)
    async with database.read_transaction() as reader:
        with await reader.execute(select(run).where(run.c.id == run_id)) as result:
            saved = result.mappings().one()
        with await reader.execute(
            select(event.c.payload).where(
                event.c.run_id == run_id, event.c.kind == "run.started"
            )
        ) as result:
            started = result.scalar_one()
        with await reader.execute(
            select(node_instance.c.node_path).where(node_instance.c.run_id == run_id)
        ) as result:
            nodes = set(result.scalars())

    assert outcome is PromotionOutcome.APPLIED
    assert (
        accepted.graph_version_id == original.graph_version_id != newer.graph_version_id
    )
    assert saved["graph_version_id"] == original.graph_version_id.removeprefix("gv_")
    assert saved["status"] == "running"
    assert saved["limits"] == {"timeout": "01h"}
    assert started["graph_version"] == saved["graph_version_id"]
    assert nodes == {"agent", "gate", "approve"}
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["missing", "overwidth", "wrong_receipt"])
async def test_scheduler_refuses_unadmitted_or_overwidth_source(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    source: str,
) -> None:
    user = await seed_case(database, NAMESPACE)
    headers = {"Authorization": str(user.request.authorization)}
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    promotion = pending_promotion(database)
    ledger = ConnectionLedger(database)
    async with client_for(app) as client:
        await deploy(client, headers, "01h")
        accepted = await trigger(client, headers)
    run_id = accepted.run_id.removeprefix("run_")
    async with database.transaction() as writer:
        match = run_promotion_admission.c.run_id == run_id
        if source == "missing":
            await writer.execute(delete(run_promotion_admission).where(match))
        elif source == "overwidth":
            await writer.execute(
                update(run_promotion_admission)
                .where(match)
                .values(run_bytes=promotion.limits.parent_bytes + 1)
            )
        else:
            receipt_id = (
                select(run_promotion_admission.c.receipt_id)
                .where(match)
                .scalar_subquery()
            )
            await writer.execute(
                update(inbound_event)
                .where(inbound_event.c.id == receipt_id)
                .values(start_outcome={"version": 1})
            )
    deadline = asyncio.get_running_loop().time() + 10.0

    outcome = await promotion.process(run_id, deadline=deadline, stopped=lambda: False)
    async with database.read_transaction() as reader:
        status = await reader.scalar(select(run.c.status).where(run.c.id == run_id))
        node = await reader.scalar(
            select(node_instance.c.id).where(node_instance.c.run_id == run_id)
        )
        started = await reader.scalar(
            select(event.c.id).where(
                event.c.run_id == run_id, event.c.kind == "run.started"
            )
        )

    assert outcome is PromotionOutcome.REFUSED
    assert status == "pending"
    assert node is None
    assert started is None
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
