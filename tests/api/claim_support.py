import hashlib
from collections.abc import AsyncGenerator
from dataclasses import replace

import pytest_asyncio
from sqlalchemy import insert, select, update

from kora.api.application import create_application
from kora.api.exchange import ApiExchange
from kora.api.worker_claims_route import WorkerClaimsRoute
from kora.api.worker_history_route import WorkerHistoryRoute
from kora.cel_eval.thread import Evaluator
from kora.conformance.capture import Capture, captured
from kora.db.connection import Database
from kora.db.tables import api_token, inbound_event, run
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.locks import TransitionLocks
from kora.engine.promotion_admission import record_manual_promotion_admission
from kora.engine.run_promotion import promotion_scopes
from kora.engine.start_records import (
    AcceptedStart,
    StartReceipt,
    prepare_manual_start,
    start_receipt_document,
)
from kora.engine.worker_claim_poll import ClaimPollLimits, WorkerClaimPoll
from kora.models.worker_claims import WorkerClaimRequest
from kora.observability import Level, LoggingSettings
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from tests.api.support import RequestIds
from tests.api.worker_support import CAPABILITIES
from tests.db.seed import Seeder
from tests.engine.claim_support import (
    HISTORY_LIMITS,
    READ_LIMITS,
    SNAPSHOT_LIMITS,
    UNCERTAINTY,
    WORKER_BYTES,
    ClaimCase,
    claim_case,
)
from tests.plugins.authentication.support import AuthorityClock

BEARER: str = "claim-worker-token"
LIMITS = ClaimPollLimits(SNAPSHOT_LIMITS, HISTORY_LIMITS, READ_LIMITS, WORKER_BYTES, 10)


def poller(database: Database, clock: AuthorityClock) -> WorkerClaimPoll:
    return WorkerClaimPoll(
        database,
        {"local": replace(CAPABILITIES, default_stall_after=UNCERTAINTY)},
        clock,
        LIMITS,
        UNCERTAINTY,
    )


async def application(
    database: Database,
    capture: Capture,
    clock: AuthorityClock,
    case: ClaimCase,
    history: WorkerHistoryRoute | None = None,
) -> ApiExchange:
    async with database.transaction() as writer:
        await writer.execute(
            update(api_token)
            .where(api_token.c.id == case.principal.token_id)
            .values(digest=hashlib.sha256(BEARER.encode()).hexdigest(), kind="service")
        )
    return create_application(
        StaticTokenAuthenticator({}, database),
        capture.diagnostics,
        clock,
        RequestIds(),
        worker_claims=WorkerClaimsRoute(poller(database, clock)),
        worker_history=history,
    )


def request(case: ClaimCase, claim_id: str | None = None) -> WorkerClaimRequest:
    return WorkerClaimRequest(
        runtime=case.query.runtime,
        incarnation_id=case.query.incarnation_id,
        execution_domain_id=case.query.execution_domain_id,
        max=1,
        claim_id=case.query.claim_id if claim_id is None else claim_id,
    )


async def admit_expiry_run(database: Database, case: ClaimCase, timeout: str) -> None:
    parent = case.binding.run
    scope = GraphResourceScope(parent.namespace_id, parent.graph_id, parent.version_id)
    identity = prepare_manual_start(parent.namespace_id, parent.graph_id, {})
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        receipt_id = seeder.new_id()
        await writer.execute(
            update(run)
            .where(run.c.id == parent.run_id)
            .values(
                trigger={
                    "kind": "manual",
                    "overlap": "allow",
                    "occurrence_key": receipt_id,
                },
                limits={"timeout": timeout},
            )
        )
        values = dict(
            (await writer.execute(select(run).where(run.c.id == parent.run_id)))
            .mappings()
            .one()
        )
        receipt: dict[str, object] = {
            "id": receipt_id,
            "namespace_id": parent.namespace_id,
            "name": f"kora.start:{parent.graph_id}",
            "idempotency_key": parent.run_id,
            "payload": {},
            "auth_kind": "bearer",
            "nonce": None,
            "received_at": case.clock.now(),
            "consumed_by": [],
            "start_outcome": start_receipt_document(
                StartReceipt(
                    version=1,
                    kind="manual",
                    namespace_id=parent.namespace_id,
                    graph_id=parent.graph_id,
                    request_digest=identity.request_digest,
                    response=AcceptedStart(
                        outcome="accepted",
                        run_id=parent.run_id,
                        graph_version_id=parent.version_id,
                        status="pending",
                    ),
                )
            ),
            **seeder.stamps(),
        }
        await writer.execute(insert(inbound_event).values(**receipt))
        locks = TransitionLocks(writer)
        await locks.acquire_run(parent.run_id)
        await locks.acquire_scopes(promotion_scopes(scope))
        await record_manual_promotion_admission(writer, locks, scope, values, receipt)


@pytest_asyncio.fixture
async def claim_api(
    database: Database, evaluator: Evaluator
) -> AsyncGenerator[tuple[ClaimCase, AuthorityClock, ApiExchange], None]:
    case = await claim_case(database, evaluator)
    clock = AuthorityClock()
    with captured(
        "api", LoggingSettings(level=Level.DEBUG), "claim-api", clock.now
    ) as capture:
        app = await application(database, capture, clock, case)
        yield case, clock, app
