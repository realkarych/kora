from datetime import timedelta
from pathlib import Path

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.client import KoraClient
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.cost_reads import read_latest_price
from kora.models.graphs import DeployResponse
from kora.models.runs import AcceptedRun
from tests.api.claim_support import BEARER, claim_application
from tests.api.deployment_cases import GRAPH, NAMESPACE, preparer
from tests.api.first_claim_deadline_support import (
    admit_after_first_claim_deadline_crossing,
)
from tests.api.scheduler_claim_support import (
    PARAMS,
    promote_and_admit_agent,
    scheduler_claim_case,
    scheduler_claim_ir,
    scheduler_claim_setup,
)
from tests.api.support import api_capture, lifespan
from tests.api.worker_support import bearer
from tests.cli.test_deploy_command import kora, written
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import state
from tests.engine.test_http_trigger_admission import KEY

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
async def test_real_scheduler_admission_hands_pinned_attempt_to_worker_claim(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    transport: str,
) -> None:
    ledger = ConnectionLedger(database)
    setup = await scheduler_claim_setup(database, preparer, api_capture)
    clock = setup.clock
    expected_deadline = clock.now() + timedelta(hours=1)
    async with database.read_transaction() as reader:
        price = await read_latest_price(reader, None, clock.now())

    async with (
        running_app(setup.app) as origin,
        KoraClient(
            origin if transport == "http" else "http://127.0.0.1",
            BEARER,
            asgi_app=setup.app if transport == "asgi" else None,
        ) as client,
    ):
        worker_id = (await client.register_worker(setup.registration)).worker_id
        safe = await admit_after_first_claim_deadline_crossing(
            origin, setup, client, worker_id, ledger, transport=transport
        )
        before_claim = await state(database)
        clock.advance(1)
        claim_at = clock.now()
        first = await client.claim_worker(worker_id, setup.request)
        before_replay = await state(database)
        clock.advance(20)
        replay = await client.claim_worker(worker_id, setup.request)
    after = await state(database)

    assert len(before_claim["attempt"]) == len(after["attempt"]) == 2
    (original,) = [row for row in before_claim["attempt"] if row["status"] == "created"]
    (claimed,) = [row for row in after["attempt"] if row["id"] == original["id"]]
    assert (original["started_at"], original["deadline"]) == (None, None)
    assert before_claim["attempt_dispatcher"] == []
    assert len(after["attempt_dispatcher"]) == 1
    assert first.claim is not None
    assert first.claim.attempt_id == original["id"]
    assert (
        first.claim.execution_context.graph_version_id
        == safe.promotion.scope.version_id
    )
    assert (
        first.claim.execution_context.execution_contract_digest
        == safe.deployed.execution_contract_digest
    )
    assert first.claim.params == PARAMS
    assert first.claim.input_bindings == {}
    assert price is not None
    assert first.claim.price_table == price
    assert first.claim.acknowledged_segment_offset == 0
    assert first.claim.permitted_actions == ("seal_request",)
    assert first.claim.lease_epoch == claimed["lease_epoch"] == 1
    lease_until = claim_at + timedelta(seconds=120)
    assert first.claim.lease_until == claimed["lease_until"] == lease_until
    assert first.claim.dispatcher_id == after["attempt_dispatcher"][0]["id"]
    assert first.claim.execution_domain_id == setup.registration.execution_domain_id
    assert claimed["execution_state"] == "unknown"
    assert claimed["started_at"] == claim_at
    deadline_record = first.claim.deadline.value
    assert deadline_record is not None
    deadline = deadline_record.deadline()
    assert (deadline.scope_kind, deadline.scope_id) == ("run", safe.run_id)
    assert deadline.at == claimed["deadline"] == expected_deadline
    assert replay == first
    assert before_replay == after
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_cli_deploy_and_trigger_reach_first_worker_claim(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    tmp_path: Path,
) -> None:
    ledger = ConnectionLedger(database)
    setup = await scheduler_claim_setup(database, preparer, api_capture)
    graph = written(tmp_path, "skeleton.ir.json", scheduler_claim_ir())

    async with running_app(setup.app) as origin, KoraClient(origin, BEARER) as worker:
        environment = {
            "KORA_URL": origin,
            "KORA_TOKEN": bearer(setup.user),
            "KORA_NAMESPACE": NAMESPACE,
        }
        deployment = await kora(["deploy", str(graph), "--json"], environment)
        assert deployment.code == 0
        deployed = DeployResponse.model_validate_json(deployment.stdout)
        start = await kora(
            [
                "trigger",
                GRAPH,
                "--json",
                "--idempotency-key",
                KEY,
                "--param",
                f"quantity={PARAMS['quantity']}",
            ],
            environment,
        )
        assert start.code == 0
        accepted = AcceptedRun.model_validate_json(start.stdout)
        pending = await state(database)
        await promote_and_admit_agent(setup, accepted, deployed)
        before_claim = await state(database)
        registered = await worker.register_worker(setup.registration)
        first = await worker.claim_worker(registered.worker_id, setup.request)
        before_replay = await state(database)
        setup.clock.advance(20)
        replay = await worker.claim_worker(registered.worker_id, setup.request)
    after = await state(database)

    run_id = accepted.run_id.removeprefix("run_")
    version_id = deployed.graph_version_id.removeprefix("gv_")
    assert accepted.graph_version_id == deployed.graph_version_id
    (pending_run,) = pending["run"]
    assert (pending_run["id"], pending_run["status"]) == (run_id, "pending")
    assert pending["attempt"] == before_claim["attempt_dispatcher"] == []
    (created,) = before_claim["attempt"]
    (claimed,) = after["attempt"]
    (saved_run,) = after["run"]
    (grant,) = after["attempt_dispatcher"]
    agent = next(
        node for node in after["node_instance"] if node["node_path"] == "agent"
    )
    assert first.claim is not None
    claim = first.claim
    assert (created["status"], created["number"]) == ("created", 1)
    assert (
        claim.attempt_id
        == created["id"]
        == claimed["id"]
        == agent["current_attempt_id"]
    )
    assert created["node_instance_id"] == claimed["node_instance_id"] == agent["id"]
    assert claim.run_id == agent["run_id"] == saved_run["id"] == run_id
    assert (
        claim.execution_context.graph_version_id
        == saved_run["graph_version_id"]
        == version_id
    )
    assert (
        claim.execution_context.execution_contract_digest
        == deployed.execution_contract_digest
    )
    assert claim.node_path == "agent"
    assert claim.params == PARAMS
    assert grant["attempt_id"] == claim.attempt_id
    assert grant["worker_id"] == claim.current_grant.worker_id == registered.worker_id
    assert (
        grant["incarnation_id"]
        == claim.current_grant.incarnation_id
        == registered.incarnation_id
    )
    assert grant["id"] == claim.dispatcher_id == claim.current_grant.dispatcher_id
    assert grant["lease_epoch"] == claim.lease_epoch == claimed["lease_epoch"] == 1
    assert grant["claim_id"] == setup.request.claim_id
    assert replay == first
    assert before_replay == after
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authored_stall", "microseconds"),
    [("0s", "0"), ("1000000000000000d", "86400000000000000000000000")],
    ids=["zero", "wide"],
)
async def test_scheduler_claim_replays_authored_stall_after_asgi_replacement(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    authored_stall: str,
    microseconds: str,
) -> None:
    case = await scheduler_claim_case(
        database, preparer, api_capture, authored_stall=authored_stall
    )
    case.clock.advance(1)
    replacement = Database(database.settings)
    ledger = ConnectionLedger(replacement)
    replacement_app = claim_application(
        replacement, api_capture, case.clock, default_stall=timedelta(seconds=23)
    )

    try:
        async with (
            lifespan(case.app),
            KoraClient("http://127.0.0.1", BEARER, asgi_app=case.app) as client,
        ):
            await client.register_worker(case.registration)
            first = await client.claim_worker(case.registration.name, case.request)
        before = await state(database)
        case.clock.advance(20)
        async with (
            lifespan(replacement_app),
            KoraClient("http://127.0.0.1", BEARER, asgi_app=replacement_app) as client,
        ):
            replay = await client.claim_worker(case.registration.name, case.request)
        after = await state(database)
    finally:
        await replacement.close()

    assert first.claim is not None
    assert first.claim.limits["stall_after_us"] == microseconds
    assert replay == first
    assert before == after
    (basis,) = after["attempt_first_claim_basis"]
    assert basis["dispatcher_id"] == first.claim.dispatcher_id
    assert basis["configured_stall_us"] is None
    assert ledger.outstanding == replacement.units_in_flight() == 0
    assert ledger.opened == ledger.closed
