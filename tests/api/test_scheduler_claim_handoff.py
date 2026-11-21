from contextlib import AsyncExitStack
from datetime import timedelta

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.client import KoraClient
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.cost_reads import read_latest_price
from tests.api.claim_support import BEARER
from tests.api.deployment_cases import preparer
from tests.api.scheduler_claim_support import PARAMS, scheduler_claim_case
from tests.api.support import api_capture
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import state

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
    case = await scheduler_claim_case(database, preparer, api_capture)
    clock = case.clock
    expected_deadline = clock.now() + timedelta(hours=1)
    async with database.read_transaction() as reader:
        price = await read_latest_price(reader, None, clock.now())

    before_claim = await state(database)
    clock.advance(1)
    claim_at = clock.now()
    async with AsyncExitStack() as resources:
        origin = (
            await resources.enter_async_context(running_app(case.app))
            if transport == "http"
            else "http://127.0.0.1"
        )
        client = await resources.enter_async_context(
            KoraClient(
                origin, BEARER, asgi_app=case.app if transport == "asgi" else None
            )
        )
        await client.register_worker(case.registration)
        first = await client.claim_worker(case.registration.name, case.request)
        before_replay = await state(database)
        clock.advance(20)
        replay = await client.claim_worker(case.registration.name, case.request)
    after = await state(database)

    assert len(before_claim["attempt"]) == len(after["attempt"]) == 1
    assert before_claim["attempt"][0]["status"] == "created"
    assert before_claim["attempt"][0]["started_at"] is None
    assert before_claim["attempt"][0]["deadline"] is None
    assert before_claim["attempt_dispatcher"] == []
    assert len(after["attempt_dispatcher"]) == 1
    assert first.claim is not None
    assert first.claim.attempt_id == before_claim["attempt"][0]["id"]
    assert (
        first.claim.execution_context.graph_version_id
        == case.promotion.scope.version_id
    )
    assert (
        first.claim.execution_context.execution_contract_digest
        == case.deployed.execution_contract_digest
    )
    assert first.claim.params == PARAMS
    assert first.claim.input_bindings == {}
    assert price is not None
    assert first.claim.price_table == price
    assert first.claim.acknowledged_segment_offset == 0
    assert first.claim.permitted_actions == ("seal_request",)
    assert first.claim.lease_epoch == after["attempt"][0]["lease_epoch"] == 1
    lease_until = claim_at + timedelta(seconds=120)
    assert first.claim.lease_until == after["attempt"][0]["lease_until"] == lease_until
    assert first.claim.dispatcher_id == after["attempt_dispatcher"][0]["id"]
    assert first.claim.execution_domain_id == case.registration.execution_domain_id
    assert after["attempt"][0]["execution_state"] == "unknown"
    assert after["attempt"][0]["started_at"] == claim_at
    deadline_record = first.claim.deadline.value
    assert deadline_record is not None
    deadline = deadline_record.deadline()
    assert (deadline.scope_kind, deadline.scope_id) == ("run", case.promotion.run_id)
    assert deadline.at == after["attempt"][0]["deadline"] == expected_deadline
    assert replay == first
    assert before_replay == after
    assert ledger.outstanding == database.units_in_flight() == 0
