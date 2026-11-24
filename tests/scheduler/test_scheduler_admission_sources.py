import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from sqlalchemy import select

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.client import KoraClient
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import wait
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.engine.run_promotion import PromotionOutcome
from kora.ir.json import JsonObject
from kora.models.errors import ApiErrorCode
from kora.models.graphs import DeployResponse
from kora.models.runs import AcceptedRun
from kora.scheduler.loop import first_claim_scheduler
from tests.api.claim_support import BEARER
from tests.api.deployment_cases import GRAPH, preparer, skeleton_ir
from tests.api.first_claim_deadline_support import refuse_claim
from tests.api.scheduler_claim_support import (
    PARAMS,
    node_id,
    scheduler_claim_ir,
    scheduler_claim_setup,
)
from tests.api.support import api_capture
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.engine.admission_processing_support import process, processor
from tests.engine.admission_snapshot_support import snapshot_case
from tests.engine.claim_support import state
from tests.engine.graph_output_cases import child
from tests.engine.test_claim_snapshot import PausedClaimRead
from tests.engine.test_http_trigger_admission import (
    KEY,
    RUNS,
    VERSIONS,
    promotion_from_http,
)
from tests.scheduler.test_first_claim_scheduler import BOUND, stop_on_sql

__all__ = ["api_capture", "preparer"]


def out_of_range_attempt_ir(*, with_earlier_run_deadline: bool) -> JsonObject:
    ir = scheduler_claim_ir()
    child(ir, "graph", "nodes", "agent")["limits"] = {"timeout": "10000000d"}
    if not with_earlier_run_deadline:
        del child(ir, "runtime")["limits"]
    return ir


async def deploy_and_trigger(
    client: httpx.AsyncClient, ir: JsonObject, key: str
) -> tuple[DeployResponse, AcceptedRun]:
    published = await client.post(
        VERSIONS, json={"graph": GRAPH, "ir": ir, "resources": []}
    )
    assert published.status_code == 201
    triggered = await client.post(
        RUNS, json={"params": PARAMS}, headers={"Idempotency-Key": key}
    )
    assert triggered.status_code == 201
    return (
        DeployResponse.model_validate(published.json()),
        AcceptedRun.model_validate(triggered.json()),
    )


@pytest.mark.asyncio
async def test_scheduler_claim_overflow_releases_only_original_reservation(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    setup = await scheduler_claim_setup(database, preparer, api_capture)
    ledger = ConnectionLedger(database)
    admission = replace(setup.processor, initial_root_only=True)
    headers = {"Authorization": str(setup.user.request.authorization)}
    ir = scheduler_claim_ir()
    cutoff = datetime.max.replace(tzinfo=UTC)
    millis = (cutoff - setup.clock.now()) // timedelta(milliseconds=1) - 1000
    child(ir, "graph", "nodes", "agent")["limits"] = {"timeout": f"{millis}ms"}
    del child(ir, "runtime")["limits"]

    async with (
        running_app(setup.app) as origin,
        httpx.AsyncClient(
            base_url=origin, headers=headers, trust_env=False, timeout=BOUND
        ) as api,
        KoraClient(origin, BEARER) as worker,
    ):
        registration = await worker.register_worker(setup.registration)
        _, accepted = await deploy_and_trigger(api, ir, "scheduler-crossing-cutoff")
        original_run_id = accepted.run_id.removeprefix("run_")
        first = first_claim_scheduler(admission, page_size=1)
        await first.recover()
        await stop_on_sql(
            first,
            database.writer_engine,
            lambda sql, _: sql.startswith("INSERT INTO attempt "),
        )
        later_version, later = await deploy_and_trigger(
            api, scheduler_claim_ir(), "scheduler-after-crossing"
        )
        later_run_id = later.run_id.removeprefix("run_")
        blocked = first_claim_scheduler(admission, page_size=1)
        await stop_on_sql(
            blocked,
            database.writer_engine,
            lambda sql, _: sql.startswith("INSERT INTO wait "),
        )
        before = await state(database)
        async with database.read_transaction() as reader:
            capacity_wait = (await reader.execute(select(wait))).mappings().one()

        setup.clock.advance(2)
        refused = await refuse_claim(worker, registration.worker_id, setup.request)
        discharged = await state(database)
        empty = await worker.claim_worker(registration.worker_id, setup.request)
        after_refusal_replay = await state(database)
        restarted = first_claim_scheduler(admission, page_size=1)
        await restarted.recover()
        await stop_on_sql(
            restarted,
            database.writer_engine,
            lambda sql, _: sql.startswith("INSERT INTO attempt "),
        )
        request = setup.request.model_copy(update={"claim_id": "scheduler-next-claim"})
        granted = await worker.claim_worker(registration.worker_id, request)
        before_grant_replay = await state(database)
        replayed = await worker.claim_worker(registration.worker_id, request)
        after_grant_replay = await state(database)

    (original,) = before["attempt"]
    (closed,) = discharged["attempt"]
    later_node = next(
        row for row in before["node_instance"] if row["run_id"] == later_run_id
    )
    reason = cast(JsonObject, capacity_wait["reason"])
    assert later_node["status"] == "waiting"
    assert capacity_wait["node_instance_id"] == later_node["id"]
    assert capacity_wait["status"] == "open"
    assert reason["scope"] == {"kind": "global", "id": None}
    assert reason["limit"] == reason["used"] == 1
    assert original["status"] == "created"
    assert closed == original | {
        "status": "cancelled",
        "desired_state": "cancel",
        "finished_at": setup.clock.now(),
        "updated_at": setup.clock.now(),
    }
    assert closed["lease_epoch"] == 0
    assert closed["execution_state"] == "not_started"
    assert all(
        closed[field] is None
        for field in (
            "worker_id",
            "execution_domain_id",
            "lease_owner",
            "lease_until",
            "handle",
            "started_at",
            "deadline",
        )
    )
    (event,) = [row for row in discharged["event"] if row not in before["event"]]
    assert (event["kind"], event["run_id"], event["attempt_id"]) == (
        "attempt.cancelled",
        original_run_id,
        original["id"],
    )
    assert (refused.status, refused.code) == (500, ApiErrorCode.INTERNAL)
    assert original_run_id not in refused.content.decode()
    assert (
        discharged["attempt_dispatcher"]
        == discharged["attempt_first_claim_basis"]
        == []
    )
    assert empty.claim is None
    (worker_at_refusal,) = discharged["worker"]
    generation = worker_at_refusal["claim_cursor_generation"]
    assert isinstance(generation, int)
    assert discharged["worker"] == before["worker"]
    assert after_refusal_replay == discharged | {
        "worker": [
            worker_at_refusal
            | {
                "claim_cursor": None,
                "claim_cursor_generation": generation + 1,
                "updated_at": setup.clock.now(),
            }
        ]
    }
    assert granted.claim is not None
    assert granted.claim.run_id == later_run_id
    assert (
        granted.claim.execution_context.graph_version_id
        == later.graph_version_id.removeprefix("gv_")
    )
    assert granted.claim.execution_context.execution_contract_digest == (
        later_version.execution_contract_digest
    )
    assert granted == replayed
    assert before_grant_replay == after_grant_replay
    assert len(after_grant_replay["attempt"]) == 2
    assert closed in after_grant_replay["attempt"]
    (dispatcher,) = after_grant_replay["attempt_dispatcher"]
    (basis,) = after_grant_replay["attempt_first_claim_basis"]
    assert dispatcher["attempt_id"] == granted.claim.attempt_id
    assert dispatcher["id"] == basis["dispatcher_id"] == granted.claim.dispatcher_id
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_scheduler_refuses_unrepresentable_root_deadline_and_admits_later_run(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    setup = await scheduler_claim_setup(database, preparer, api_capture)
    admission = replace(setup.processor, initial_root_only=True)
    owner = first_claim_scheduler(admission, page_size=1)
    headers = {"Authorization": str(setup.user.request.authorization)}
    namespace = setup.user.principal.namespaces[0].namespace_id

    async with running_app(setup.app) as origin:
        async with httpx.AsyncClient(
            base_url=origin, headers=headers, trust_env=False, timeout=BOUND
        ) as api:
            deployed, accepted = await deploy_and_trigger(
                api, out_of_range_attempt_ir(with_earlier_run_deadline=False), KEY
            )
            promotion, _ = await promotion_from_http(
                database,
                namespace,
                accepted,
                deployed,
                setup.clock,
                admission.identifiers,
            )
            assert await promotion.promote(database) is PromotionOutcome.APPLIED
            unsupported_root = await node_id(database, promotion.run_id, "agent")
            refusal = await process(admission, unsupported_root)
            before_supported = await state(database)
            later_version, later = await deploy_and_trigger(
                api,
                out_of_range_attempt_ir(with_earlier_run_deadline=True),
                "scheduler-supported-deadline",
            )
        await owner.recover()
        await stop_on_sql(
            owner,
            database.writer_engine,
            lambda sql, _: sql.lower().startswith("insert into attempt "),
        )
        async with KoraClient(origin, BEARER) as worker:
            await worker.register_worker(setup.registration)
            response = await worker.claim_worker(setup.registration.name, setup.request)
        claimed = await state(database)

    assert refusal is AttemptAdmissionState.REFUSED
    assert before_supported["attempt"] == before_supported["attempt_dispatcher"] == []
    assert not before_supported["attempt_claim_read_admission"]
    assert not before_supported["attempt_first_claim_basis"]
    assert len(claimed["attempt"]) == len(claimed["attempt_dispatcher"]) == 1
    assert response.claim is not None
    assert response.claim.attempt_id == claimed["attempt"][0]["id"]
    assert response.claim.run_id == later.run_id.removeprefix("run_")
    assert response.claim.execution_context.execution_contract_digest == (
        later_version.execution_contract_digest
    )
    deadline = response.claim.deadline.value
    assert deadline is not None
    assert deadline.scope_kind == "run"
    assert deadline.scope_id == response.claim.run_id
    assert datetime.fromisoformat(deadline.at) == setup.clock.now() + timedelta(hours=1)
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_scheduler_refuses_stale_graph_caps_then_admits_with_current_proof(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    owner = replace(processor(database, case), initial_root_only=True)
    proposed = skeleton_ir()
    child(proposed, "runtime")["concurrency"] = {"max_running_nodes": 2}
    before = await case.promotion.snapshot(database)

    with PausedClaimRead(database) as pause:
        pending = asyncio.create_task(process(owner, case.node_id))
        try:
            await asyncio.wait_for(pause.entered.wait(), BOUND)
            await case.publish(proposed)
        finally:
            pause.release.set()
            outdated = await asyncio.wait_for(pending, BOUND)
    after_refusal = await case.promotion.snapshot(database)
    before_fresh = await state(database)
    current = await process(owner, case.node_id)
    after_fresh = await state(database)

    assert outdated is AttemptAdmissionState.REFUSED
    assert after_refusal == before
    assert before_fresh["attempt"] == before_fresh["attempt_claim_read_admission"] == []
    assert current is AttemptAdmissionState.QUEUED
    assert (
        len(after_fresh["attempt"])
        == len(after_fresh["attempt_claim_read_admission"])
        == 1
    )
    assert after_fresh["attempt"][0]["node_instance_id"] == case.node_id
    assert not after_fresh["attempt_dispatcher"]
    assert database.units_in_flight() == 0
