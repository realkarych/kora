import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
from hashlib import sha256
from typing import cast

import pytest
from sqlalchemy import select, update

from kora.api.application import create_application
from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.api.worker_claims_route import WorkerClaimsRoute
from kora.api.worker_registration_route import WorkerRegistrationRoute
from kora.client import KoraClient
from kora.conformance.authentication import AuthenticationCase
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.cost_reads import read_latest_price
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import api_token, node_instance
from kora.engine.admission_initialization import AdmissionInitialization
from kora.engine.admission_processing import AdmissionProcessor
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.engine.namespace_policy import NamespacePolicyConfiguration
from kora.engine.run_promotion import PromotionOutcome
from kora.engine.worker_claim_poll import WorkerClaimPoll
from kora.ir.json import JsonObject
from kora.models.graphs import DeployResponse
from kora.models.runs import AcceptedRun
from kora.models.worker_claims import WorkerClaimRequest
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from kora.protocols.authentication import NamespaceGrant, Principal, RequestInfo
from tests.api.claim_support import BEARER
from tests.api.claim_support import LIMITS as CLAIM_LIMITS
from tests.api.deployment_cases import GRAPH, HARNESS, NAMESPACE, preparer, skeleton_ir
from tests.api.deployment_cases import SELECTION as INSTALLATION
from tests.api.support import RequestIds, api_capture, client_for, deployment_services
from tests.api.worker_support import CAPABILITIES, registration
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.db.seed import FakeClock, Seeder, fixed_randomness
from tests.engine.admission_processing_support import LIMITS as ADMISSION_LIMITS
from tests.engine.admission_wait_support import INTERVAL
from tests.engine.attempt_admission_support import SELECTION
from tests.engine.claim_support import UNCERTAINTY, state
from tests.engine.test_http_trigger_admission import (
    KEY,
    RUNS,
    VERSIONS,
    promotion_from_http,
)
from tests.plugins.authentication.support import AuthorityClock, seed_case

__all__ = ["api_capture", "preparer"]


async def service_identity(database: Database, namespace_id: str) -> AuthenticationCase:
    with_grant = NamespaceGrant(namespace_id=namespace_id, transcripts=False)
    owner = "scheduler-handoff-worker"
    async with database.transaction() as writer:
        token_id = await Seeder(writer).token(
            sha256(BEARER.encode()).hexdigest(), kind="service", owner=owner
        )
        await writer.execute(
            update(api_token)
            .where(api_token.c.id == token_id)
            .values(
                namespaces=[with_grant.model_dump()], approve=False, runtime="local"
            )
        )
    return AuthenticationCase(
        RequestInfo("Bearer " + BEARER),
        Principal(
            token_id=token_id,
            kind="service",
            owner=owner,
            namespaces=(with_grant,),
            approve=False,
            platform=False,
            runtime="local",
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
async def test_real_scheduler_admission_hands_pinned_attempt_to_worker_claim(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    transport: str,
) -> None:
    user = await seed_case(database, NAMESPACE)
    namespace_id = user.principal.namespaces[0].namespace_id
    worker = await service_identity(database, namespace_id)
    worker_registration = registration(worker)
    request = WorkerClaimRequest(
        runtime=worker_registration.runtime,
        incarnation_id=worker_registration.incarnation_id,
        execution_domain_id=worker_registration.execution_domain_id,
        max=1,
        claim_id="scheduler-handoff-claim",
    )
    clock = FakeClock()
    api_clock = AuthorityClock(clock.now())
    identifiers = IdentifierGenerator(clock.now, fixed_randomness(37))
    policy = NamespacePolicyConfiguration.prepare(
        namespace_id, 1, {"max_pending_runs": 1000}
    )
    await AdmissionInitialization(
        database, api_clock, identifiers.new, policies=(policy,)
    ).initialize(api_capture.diagnostics)
    capabilities = {
        "local": replace(
            CAPABILITIES, default_stall_after=SELECTION.default_stall_after
        )
    }
    app = create_application(
        StaticTokenAuthenticator({}, database),
        api_capture.diagnostics,
        api_clock,
        RequestIds(),
        deployment_services(database, preparer, HARNESS),
        worker_registration=WorkerRegistrationRoute(
            database, capabilities, api_clock, api_capture.diagnostics
        ),
        worker_claims=WorkerClaimsRoute(
            WorkerClaimPoll(
                database, capabilities, api_clock, CLAIM_LIMITS, UNCERTAINTY
            )
        ),
    )
    owner = AdmissionProcessor(
        database,
        ADMISSION_LIMITS,
        SELECTION,
        INSTALLATION,
        clock.now,
        identifiers,
        1,
        1,
        INTERVAL,
    )
    ledger = ConnectionLedger(database)
    ir = skeleton_ir()
    cast(JsonObject, ir["runtime"])["limits"] = {"timeout": "1h"}
    cast(JsonObject, ir["graph"])["params"] = {
        "type": "object",
        "properties": {"quantity": {"type": "integer"}},
        "required": ["quantity"],
        "additionalProperties": False,
    }
    params = {"quantity": 7}
    headers = {"Authorization": str(user.request.authorization)}

    async with client_for(app) as deployment_client:
        publication = await deployment_client.post(
            VERSIONS, json={"graph": GRAPH, "ir": ir, "resources": []}, headers=headers
        )
        deployed = DeployResponse.model_validate(publication.json())
        start = await deployment_client.post(
            RUNS,
            json={"params": params},
            headers=headers | {"Idempotency-Key": KEY},
        )
        accepted = AcceptedRun.model_validate(start.json())
    clock.advance(1)
    promotion, _ = await promotion_from_http(
        database, namespace_id, accepted, deployed, clock, identifiers
    )
    promoted = await promotion.promote(database)
    async with database.read_transaction() as reader:
        node_id = await reader.scalar(
            select(node_instance.c.id).where(
                node_instance.c.run_id == promotion.run_id,
                node_instance.c.node_path == "agent",
            )
        )
        price = await read_latest_price(reader, None, clock.now())
    assert isinstance(node_id, str)
    admitted = await owner.process(
        node_id, periodic_recheck=False, deadline=asyncio.get_running_loop().time() + 10
    )
    before_claim = await state(database)
    clock.advance(1)
    api_clock.moment = clock.now()
    async with AsyncExitStack() as resources:
        origin = (
            await resources.enter_async_context(running_app(app))
            if transport == "http"
            else "http://127.0.0.1"
        )
        client = await resources.enter_async_context(
            KoraClient(origin, BEARER, asgi_app=app if transport == "asgi" else None)
        )
        await client.register_worker(worker_registration)
        first = await client.claim_worker(worker_registration.name, request)
        before_replay = await state(database)
        replay = await client.claim_worker(worker_registration.name, request)
    after = await state(database)

    assert publication.status_code == start.status_code == 201
    assert promoted is PromotionOutcome.APPLIED
    assert admitted is AttemptAdmissionState.QUEUED
    assert len(before_claim["attempt"]) == len(after["attempt"]) == 1
    assert before_claim["attempt"][0]["status"] == "created"
    assert before_claim["attempt"][0]["started_at"] is None
    assert before_claim["attempt_dispatcher"] == []
    assert len(after["attempt_dispatcher"]) == 1
    assert first.claim is not None
    assert first.claim.attempt_id == before_claim["attempt"][0]["id"]
    assert first.claim.execution_context.graph_version_id == promotion.scope.version_id
    assert (
        first.claim.execution_context.execution_contract_digest
        == deployed.execution_contract_digest
    )
    assert first.claim.params == params
    assert first.claim.input_bindings == {}
    assert price is not None
    assert first.claim.price_table == price
    assert first.claim.acknowledged_segment_offset == 0
    assert first.claim.permitted_actions == ("seal_request",)
    assert after["attempt"][0]["lease_epoch"] == 1
    assert after["attempt"][0]["execution_state"] == "unknown"
    assert after["attempt"][0]["started_at"] == clock.now()
    assert after["attempt"][0]["deadline"] is not None
    assert replay == first
    assert before_replay == after
    assert ledger.outstanding == database.units_in_flight() == 0
