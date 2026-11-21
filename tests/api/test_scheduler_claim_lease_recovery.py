import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.util import await_

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.client import KoraClient
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.deadline_discovery import DueAttemptSweep
from kora.db.reads import has_physical_attempt
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.engine.lease_recovery import recover_expired_lease
from kora.models.worker_claims import WorkerClaim
from kora.protocols.worker_heartbeats import HeartbeatResponse, WorkerHeartbeat
from tests.api.claim_support import BEARER
from tests.api.deployment_cases import preparer
from tests.api.scheduler_claim_support import (
    SchedulerClaimCase,
    node_id,
    scheduler_claim_case,
)
from tests.api.support import api_capture
from tests.client.support import BOUND, running_app
from tests.db.closing import ConnectionLedger
from tests.engine.admission_processing_support import process
from tests.engine.claim_support import state
from tests.engine.expiry_support import expiry_context

__all__ = ["api_capture", "preparer"]


async def due_leases(case: SchedulerClaimCase) -> tuple[tuple[str, str], ...]:
    async with case.processor.database.read_transaction() as reader:
        return await DueAttemptSweep("lease").next_page(reader, case.clock.now())


async def recover_lease(case: SchedulerClaimCase, attempt_id: str) -> bool:
    async with case.processor.database.transaction() as writer:
        authority = await expiry_context(writer, case.promotion, capacity=False)
        return await recover_expired_lease(authority, attempt_id)


async def scan_during_heartbeat_commit(
    case: SchedulerClaimCase,
    client: KoraClient,
    grant: WorkerClaim,
) -> tuple[tuple[tuple[str, str], ...], HeartbeatResponse]:
    entered = asyncio.Event()
    release = asyncio.Event()
    engine = case.processor.database.writer_engine.sync_engine
    heartbeat_writer: Connection | None = None

    def record_renewal(
        connection: Connection, cursor: object, statement: str, *arguments: object
    ) -> None:
        nonlocal heartbeat_writer
        if (
            statement.startswith("UPDATE attempt SET")
            and "last_heartbeat_at=" in statement
        ):
            heartbeat_writer = connection

    def hold_commit(connection: Connection) -> None:
        if connection is heartbeat_writer:
            entered.set()
            await_(release.wait())

    body = WorkerHeartbeat(
        dispatcher_id=grant.dispatcher_id, lease_epoch=grant.lease_epoch
    )
    event.listen(engine, "after_cursor_execute", record_renewal)
    event.listen(engine, "commit", hold_commit)
    try:
        async with asyncio.timeout(BOUND), asyncio.TaskGroup() as tasks:
            heartbeat = tasks.create_task(
                client.heartbeat_attempt(grant.attempt_id, body)
            )
            try:
                await entered.wait()
                case.clock.advance(60)
                candidates = await due_leases(case)
            finally:
                release.set()
        return candidates, heartbeat.result()
    finally:
        event.remove(engine, "commit", hold_commit)
        event.remove(engine, "after_cursor_execute", record_renewal)


@pytest.mark.asyncio
async def test_recovery_rechecks_a_committed_heartbeat_after_due_scan(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
) -> None:
    ledger = ConnectionLedger(database)
    case = await scheduler_claim_case(
        database, preparer, api_capture, with_heartbeat=True
    )
    run_id = case.promotion.run_id
    gate = await node_id(database, run_id, "gate")
    case.clock.advance(1)
    claim_at = case.clock.now()
    async with (
        running_app(case.app) as origin,
        KoraClient(origin, BEARER) as client,
    ):
        await client.register_worker(case.registration)
        grant = (await client.claim_worker(case.registration.name, case.request)).claim
        assert grant is not None
        case.clock.advance(60)

        candidates, heartbeat = await scan_during_heartbeat_commit(case, client, grant)
        renewed = await state(database)
        stale_recovery = await recover_lease(case, grant.attempt_id)
        after_stale = await state(database)
        case.clock.advance(60)
        expired_candidates = await due_leases(case)
        recovery = await recover_lease(case, grant.attempt_id)
        first_recovery = await state(database)
        replay = await recover_lease(case, grant.attempt_id)
        gate_admission = await process(case.processor, gate, periodic=False)
        saved = await state(database)
        async with database.read_transaction() as reader:
            occupied = await has_physical_attempt(reader, run_id)

    assert grant.lease_until == claim_at + timedelta(seconds=120)
    assert candidates == expired_candidates == ((grant.attempt_id, run_id),)
    renewed_until = claim_at + timedelta(seconds=180)
    assert (
        heartbeat.lease_until == renewed["attempt"][0]["lease_until"] == renewed_until
    )
    assert not stale_recovery
    assert after_stale == renewed
    assert recovery
    assert not replay
    assert occupied
    assert gate_admission is AttemptAdmissionState.REFUSED
    assert first_recovery == saved
    assert len(saved["attempt"]) == 1
    original = saved["attempt"][0]
    assert original["id"] == grant.attempt_id
    assert original["status"] == "claimed"
    assert original["execution_domain_id"] == grant.execution_domain_id
    assert original["lease_owner"] is original["lease_until"] is None
    assert original["lease_epoch"] == grant.lease_epoch
    assert original["execution_state"] == "unknown"
    assert original["recovery_required"] is True
    version = renewed["attempt"][0]["recovery_version"]
    assert isinstance(version, int)
    assert original["recovery_version"] == version + 1
    assert saved["attempt_dispatcher"] == renewed["attempt_dispatcher"]
    assert [row["kind"] for row in saved["event"]].count(
        "attempt.recovery_required"
    ) == 1
    assert (
        next(row for row in saved["node_instance"] if row["id"] == gate)["status"]
        == "pending"
    )
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
