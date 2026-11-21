import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Connection
from sqlalchemy.util import await_

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.client import KoraClient
from kora.conformance.capture import Capture
from kora.db.attempt_capacity import AttemptCapacity, read_attempt_capacity
from kora.db.connection import Database
from kora.db.deadline_discovery import DueAttemptSweep
from kora.db.tables import attempt
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


async def attempt_capacity(case: SchedulerClaimCase, runtime: str) -> AttemptCapacity:
    promotion = case.promotion
    async with case.processor.database.read_transaction() as reader:
        return await read_attempt_capacity(
            reader,
            namespace_id=promotion.scope.namespace_id,
            graph_id=promotion.scope.graph_id,
            run_id=promotion.run_id,
            runtime=runtime,
        )


def assert_database_idle(database: Database, ledger: ConnectionLedger) -> None:
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


async def scan_during_heartbeat_commit(
    case: SchedulerClaimCase,
    client: KoraClient,
    grant: WorkerClaim,
) -> tuple[tuple[tuple[str, str], ...], datetime, HeartbeatResponse]:
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
                async with case.processor.database.read_snapshot() as reader:
                    candidates = await DueAttemptSweep("lease").next_page(
                        reader, case.clock.now()
                    )
                    before_commit = await reader.scalar(
                        select(attempt.c.lease_until).where(
                            attempt.c.id == grant.attempt_id
                        )
                    )
            finally:
                release.set()
        assert isinstance(before_commit, datetime)
        return candidates, before_commit, heartbeat.result()
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
        worker = await client.register_worker(case.registration)
        grant = (await client.claim_worker(case.registration.name, case.request)).claim
        assert grant is not None
        claimed = await state(database)
        grant_binding = grant.current_grant
        runtime = grant.execution_context.runtime
        occupied_before = await attempt_capacity(case, runtime)
        case.clock.advance(60)

        candidates, before_commit, heartbeat = await scan_during_heartbeat_commit(
            case, client, grant
        )
        renewed = await state(database)
        expected_candidates = ((grant.attempt_id, run_id),)
        assert candidates == expected_candidates
        stale_recovery = await recover_lease(case, candidates[0][0])
        assert_database_idle(database, ledger)
        after_stale = await state(database)
        case.clock.advance(60)
        expired_candidates = await due_leases(case)
        assert expired_candidates == expected_candidates
        recovery = await recover_lease(case, expired_candidates[0][0])
        assert_database_idle(database, ledger)
        first_recovery = await state(database)
        occupied_after = await attempt_capacity(case, runtime)
        replay = await recover_lease(case, grant.attempt_id)
        gate_admission = await process(case.processor, gate, periodic=False)
        saved = await state(database)

    assert grant.lease_until == before_commit == claim_at + timedelta(seconds=120)
    assert grant_binding.worker_id == worker.worker_id
    assert grant_binding.incarnation_id == worker.incarnation_id
    assert runtime == case.registration.runtime
    (initial_attempt,) = claimed["attempt"]
    (initial_dispatcher,) = claimed["attempt_dispatcher"]
    assert initial_attempt["id"] == grant.attempt_id
    assert initial_attempt["status"] == "claimed"
    assert (
        initial_attempt["worker_id"]
        == initial_attempt["lease_owner"]
        == worker.worker_id
    )
    assert initial_attempt["lease_until"] == grant.lease_until
    assert initial_attempt["lease_epoch"] == grant.lease_epoch
    assert initial_attempt["runtime"] == runtime
    assert initial_attempt["execution_domain_id"] == grant.execution_domain_id
    assert initial_attempt["current_dispatcher_open"] is True
    assert initial_attempt["recovery_required"] is False
    assert initial_dispatcher["id"] == grant.dispatcher_id
    assert initial_dispatcher["attempt_id"] == grant.attempt_id
    assert initial_dispatcher["worker_id"] == worker.worker_id
    assert initial_dispatcher["incarnation_id"] == worker.incarnation_id
    assert initial_dispatcher["lease_epoch"] == grant.lease_epoch
    assert initial_dispatcher["execution_domain_id"] == grant.execution_domain_id
    assert initial_dispatcher["closed_at"] is initial_dispatcher["evidence"] is None

    renewed_at = claim_at + timedelta(seconds=60)
    renewed_until = claim_at + timedelta(seconds=180)
    assert heartbeat.lease_until == renewed_until
    assert renewed["attempt"] == [
        initial_attempt
        | {
            "lease_until": renewed_until,
            "last_heartbeat_at": renewed_at,
            "updated_at": renewed_at,
        }
    ]
    assert renewed["attempt_dispatcher"] == claimed["attempt_dispatcher"]
    assert renewed["event"] == claimed["event"]
    assert all(
        row["kind"] not in ("attempt.execution_unknown", "attempt.recovery_required")
        for row in renewed["event"]
    )
    assert not stale_recovery
    assert after_stale == renewed

    assert recovery
    assert occupied_before.runtime_attempts == occupied_before.run_attempts == 1
    assert occupied_after == occupied_before
    assert not replay
    assert gate_admission is AttemptAdmissionState.REFUSED
    assert first_recovery == saved
    (original,) = saved["attempt"]
    assert original["id"] == grant.attempt_id
    assert original["status"] == "claimed"
    assert original["worker_id"] == worker.worker_id
    assert original["runtime"] == runtime
    assert original["execution_domain_id"] == grant.execution_domain_id
    assert original["lease_owner"] is original["lease_until"] is None
    assert original["lease_epoch"] == grant.lease_epoch
    assert original["execution_state"] == "unknown"
    assert original["current_dispatcher_open"] is True
    assert original["recovery_required"] is True
    version = initial_attempt["recovery_version"]
    assert isinstance(version, int)
    assert original["recovery_version"] == version + 1
    assert saved["attempt_dispatcher"] == claimed["attempt_dispatcher"]

    previous_events = len(renewed["event"])
    assert saved["event"][:previous_events] == renewed["event"]
    unknown, required = saved["event"][previous_events:]
    assert unknown["kind"] == "attempt.execution_unknown"
    assert required["kind"] == "attempt.recovery_required"
    assert required["payload"] == {
        "number": original["number"],
        "reasons": ["lease_expired"],
    }
    for recovery_event in (unknown, required):
        assert recovery_event["run_id"] == run_id
        assert recovery_event["attempt_id"] == grant.attempt_id
        assert recovery_event["at"] == renewed_until
    assert (
        next(row for row in saved["node_instance"] if row["id"] == gate)["status"]
        == "pending"
    )
    assert_database_idle(database, ledger)
