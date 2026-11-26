from dataclasses import dataclass, replace
from hashlib import sha256
from typing import cast

from sqlalchemy import select, update

from kora.api.application import create_application
from kora.api.exchange import ApiExchange
from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.api.worker_claims_route import WorkerClaimsRoute
from kora.api.worker_heartbeat_route import WorkerHeartbeatRoute
from kora.api.worker_registration_route import WorkerRegistrationRoute
from kora.conformance.authentication import AuthenticationCase
from kora.conformance.capture import Capture
from kora.db.connection import Database
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
from kora.models.workers import WorkerRegistration
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from kora.protocols.authentication import NamespaceGrant, Principal, RequestInfo
from kora.protocols.time import SystemClock
from tests.api.claim_support import BEARER
from tests.api.claim_support import LIMITS as CLAIM_LIMITS
from tests.api.deployment_cases import GRAPH, HARNESS, NAMESPACE, skeleton_ir
from tests.api.deployment_cases import SELECTION as INSTALLATION
from tests.api.support import RequestIds, client_for, deployment_services
from tests.api.worker_support import CAPABILITIES, registration
from tests.db.seed import FakeClock, Seeder, fixed_randomness
from tests.engine.admission_processing_support import LIMITS as ADMISSION_LIMITS
from tests.engine.admission_processing_support import process
from tests.engine.admission_wait_support import INTERVAL
from tests.engine.attempt_admission_support import SELECTION
from tests.engine.claim_support import UNCERTAINTY, WORKER_BYTES
from tests.engine.heartbeat_support import HEARTBEAT_LIMITS
from tests.engine.promotion_support import READ_LIMITS, PromotionCase
from tests.engine.test_http_trigger_admission import (
    KEY,
    RUNS,
    VERSIONS,
    promotion_from_http,
)
from tests.plugins.authentication.support import seed_case

PARAMS: JsonObject = {"quantity": 7}


class SchedulerClaimClock(FakeClock, SystemClock):
    pass


@dataclass(frozen=True)
class SchedulerClaimSetup:
    app: ApiExchange
    user: AuthenticationCase
    clock: SchedulerClaimClock
    processor: AdmissionProcessor
    registration: WorkerRegistration
    request: WorkerClaimRequest


@dataclass(frozen=True)
class SchedulerClaimCase:
    app: ApiExchange
    clock: SchedulerClaimClock
    promotion: PromotionCase
    processor: AdmissionProcessor
    deployed: DeployResponse
    registration: WorkerRegistration
    request: WorkerClaimRequest


async def service_identity(
    database: Database,
    namespace_id: str,
    *,
    credential: str = BEARER,
    owner: str = "scheduler-handoff-worker",
) -> AuthenticationCase:
    with_grant = NamespaceGrant(namespace_id=namespace_id, transcripts=False)
    async with database.transaction() as writer:
        token_id = await Seeder(writer).token(
            sha256(credential.encode()).hexdigest(), kind="service", owner=owner
        )
        await writer.execute(
            update(api_token)
            .where(api_token.c.id == token_id)
            .values(
                namespaces=[with_grant.model_dump()], approve=False, runtime="local"
            )
        )
    return AuthenticationCase(
        RequestInfo("Bearer " + credential),
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


async def node_id(database: Database, run_id: str, path: str) -> str:
    async with database.read_transaction() as reader:
        value = await reader.scalar(
            select(node_instance.c.id).where(
                node_instance.c.run_id == run_id,
                node_instance.c.node_path == path,
            )
        )
    assert isinstance(value, str)
    return value


async def scheduler_claim_setup(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    *,
    with_heartbeat: bool = False,
) -> SchedulerClaimSetup:
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
    clock = SchedulerClaimClock()
    identifiers = IdentifierGenerator(clock.now, fixed_randomness(37))
    policy = NamespacePolicyConfiguration.prepare(
        namespace_id, 1, {"max_pending_runs": 1000}
    )
    await AdmissionInitialization(
        database, clock, identifiers.new, policies=(policy,)
    ).initialize(api_capture.diagnostics)
    capabilities = {
        "local": replace(
            CAPABILITIES, default_stall_after=SELECTION.default_stall_after
        )
    }
    app = create_application(
        StaticTokenAuthenticator({}, database),
        api_capture.diagnostics,
        clock,
        RequestIds(),
        deployment_services(database, preparer, HARNESS),
        worker_registration=WorkerRegistrationRoute(
            database, capabilities, clock, api_capture.diagnostics
        ),
        worker_claims=WorkerClaimsRoute(
            WorkerClaimPoll(database, capabilities, clock, CLAIM_LIMITS, UNCERTAINTY)
        ),
        worker_heartbeat=WorkerHeartbeatRoute(
            database,
            HEARTBEAT_LIMITS,
            READ_LIMITS,
            clock,
            identifiers,
            worker_bytes=WORKER_BYTES,
            read_timeout=10.0,
            lock_timeout_ms=1000,
        )
        if with_heartbeat
        else None,
    )
    processor = AdmissionProcessor(
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
    return SchedulerClaimSetup(
        app, user, clock, processor, worker_registration, request
    )


def scheduler_claim_ir(authored_stall: str | None = None) -> JsonObject:
    ir = skeleton_ir()
    runtime = cast(JsonObject, ir["runtime"])
    runtime["limits"] = {"timeout": "1h"}
    if authored_stall is not None:
        runtime["stall_after"] = authored_stall
    cast(JsonObject, ir["graph"])["params"] = {
        "type": "object",
        "properties": {"quantity": {"type": "integer"}},
        "required": ["quantity"],
        "additionalProperties": False,
    }
    return ir


async def promote_and_admit_agent(
    setup: SchedulerClaimSetup, accepted: AcceptedRun, deployed: DeployResponse
) -> PromotionCase:
    database = setup.processor.database
    namespace_id = setup.user.principal.namespaces[0].namespace_id
    setup.clock.advance(1)
    promotion, _ = await promotion_from_http(
        database,
        namespace_id,
        accepted,
        deployed,
        setup.clock,
        setup.processor.identifiers,
    )
    assert await promotion.promote(database) is PromotionOutcome.APPLIED
    agent = await node_id(database, promotion.run_id, "agent")
    assert (
        await process(setup.processor, agent, periodic=False)
        is AttemptAdmissionState.QUEUED
    )
    return promotion


async def scheduler_claim_case(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    *,
    with_heartbeat: bool = False,
    authored_stall: str | None = None,
) -> SchedulerClaimCase:
    setup = await scheduler_claim_setup(
        database, preparer, api_capture, with_heartbeat=with_heartbeat
    )
    headers = {"Authorization": str(setup.user.request.authorization)}
    async with client_for(setup.app) as deployment_client:
        publication = await deployment_client.post(
            VERSIONS,
            json={
                "graph": GRAPH,
                "ir": scheduler_claim_ir(authored_stall),
                "resources": [],
            },
            headers=headers,
        )
        start = await deployment_client.post(
            RUNS,
            json={"params": PARAMS},
            headers=headers | {"Idempotency-Key": KEY},
        )
    assert publication.status_code == start.status_code == 201
    deployed = DeployResponse.model_validate(publication.json())
    accepted = AcceptedRun.model_validate(start.json())
    promotion = await promote_and_admit_agent(setup, accepted, deployed)
    return SchedulerClaimCase(
        setup.app,
        setup.clock,
        promotion,
        setup.processor,
        deployed,
        setup.registration,
        setup.request,
    )
