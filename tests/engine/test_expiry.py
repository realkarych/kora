import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update

from kora.db.connection import Database
from kora.db.deadline_discovery import DueAttemptSweep
from kora.db.tables import (
    approval_request,
    attempt,
    attempt_dispatcher,
    node_instance,
    run_promotion_admission,
    wait,
)
from kora.engine.deadline_records import deadline_document, prepare_deadline_record
from kora.engine.expiry import expire_run, observe_attempt_expiry, observe_run_expiry
from kora.engine.expiry_context import ExpiryFailed, ExpiryState
from kora.engine.expiry_reads import timeout_reached
from kora.engine.lease_recovery import close_undispatched_attempt, recover_expired_lease
from kora.ir.json import parse_json_object
from kora.protocols.deadlines import ExecutionDeadline
from tests.db.seed import Seeder
from tests.engine.expiry_support import ExecutionCase, execution_case, expiry_context
from tests.engine.promotion_support import promotion_case


@pytest.mark.parametrize(
    ("timeout", "elapsed", "expired"),
    [
        ("1ms", 999, False),
        ("1ms", 1000, True),
        ("1s", 999999, False),
        ("0s", 0, True),
        (None, 10**12, False),
        ("0" * 5000 + "1s", 1_000_000, True),
        ("9" * 5000 + "d", 10**12, False),
        ("01m", 60_000_000, True),
        ("1h", 3_600_000_000, True),
        ("1d", 86_400_000_000, True),
    ],
)
def test_timeout_comparison_preserves_exact_accepted_duration(
    timeout: str | None, elapsed: int, expired: bool
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)

    result = timeout_reached(start, start + timedelta(microseconds=elapsed), timeout)

    assert result is expired
    assert not timeout_reached(start, start - timedelta(microseconds=1), timeout)


@pytest.mark.asyncio
async def test_deadline_equality_latches_before_effects(database: Database) -> None:
    case = await execution_case(database)
    before = await case.snapshot(database)
    case.promotion.clock.advance(60)

    async with database.transaction() as writer:
        state = await observe_attempt_expiry(
            await case.context(writer, False), case.attempt_id
        )
    after = await case.snapshot(database)
    saved, nodes, events = await case.promotion.snapshot(database)

    assert state is ExpiryState.EXPIRED
    assert after["deadline_expired_at"] == case.promotion.clock.now()
    assert after["desired_state"] == "cancel"
    assert after["cancel_cause"] == "timeout"
    assert after["status"] == "running"
    assert after["started_at"] == before["started_at"]
    assert after["deadline_binding"] == before["deadline_binding"]
    assert nodes[0]["current_attempt_id"] == case.attempt_id
    assert saved["status"] == "running"
    assert saved["needs_scheduling"] is True
    assert [row["kind"] for row in events].count("attempt.recovery_required") == 1


@pytest.mark.asyncio
async def test_expiry_preserves_unknown_attempt_reservations(
    database: Database,
) -> None:
    case = await execution_case(database, timeout="60s")
    before = await case.snapshot(database)
    case.promotion.clock.advance(60)

    async with database.transaction() as writer:
        observed = await observe_run_expiry(await case.context(writer, False))
    observed_run, _, _ = await case.promotion.snapshot(database)
    case.promotion.clock.advance(-120)
    async with database.transaction() as writer:
        applied = await expire_run(await case.context(writer))
    after = await case.snapshot(database)
    saved, nodes, events = await case.promotion.snapshot(database)

    assert observed is applied is ExpiryState.EXPIRED
    assert observed_run["status"] == "running"
    assert observed_run["deadline_expired_at"] is not None
    assert saved["status"] == "failed"
    assert saved["target_held_since"] is not None
    assert after["status"] == "running"
    assert after["desired_state"] == "cancel"
    for field in (
        "execution_domain_id",
        "started_at",
        "lease_epoch",
        "lease_until",
        "input_bindings",
        "execution_request",
        "infrastructure_retries",
    ):
        assert after[field] == before[field]
    assert all(node["status"] == "cancelled" for node in nodes)
    failed = [row for row in events if row["kind"] == "run.failed"]
    assert len(failed) == 1
    assert failed[0]["payload"] == {
        "trigger": observed_run["trigger"],
        "graph_version": case.promotion.scope.version_id,
        "rerun_of": None,
        "deadline": {"scope": "run", "reason": "timeout"},
    }


@pytest.mark.asyncio
async def test_earlier_cancel_cause_survives_timeout(database: Database) -> None:
    case = await execution_case(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == case.attempt_id)
            .values(desired_state="cancel", cancel_cause="clear")
        )
    case.promotion.clock.advance(60)

    async with database.transaction() as writer:
        result = await observe_attempt_expiry(
            await case.context(writer, False), case.attempt_id
        )
    case.promotion.clock.advance(-120)
    async with database.transaction() as writer:
        replayed = await observe_attempt_expiry(
            await case.context(writer, False), case.attempt_id
        )
    saved = await case.snapshot(database)

    assert result is replayed is ExpiryState.EXPIRED
    assert saved["cancel_cause"] == "clear"
    assert saved["deadline_expired_at"] is not None


@pytest.mark.asyncio
async def test_expired_lease_retains_original_attempt_and_domain(
    database: Database,
) -> None:
    case = await execution_case(database)
    before = await case.snapshot(database)
    case.promotion.clock.advance(30)

    async with database.transaction() as writer:
        changed = await recover_expired_lease(
            await case.context(writer, False), case.attempt_id
        )
    first = await case.snapshot(database)
    async with database.transaction() as writer:
        repeated = await recover_expired_lease(
            await case.context(writer, False), case.attempt_id
        )
    second = await case.snapshot(database)
    saved, _, events = await case.promotion.snapshot(database)

    assert changed
    assert not repeated
    assert first == second
    assert first["status"] == "running"
    assert first["execution_state"] == "unknown"
    assert first["lease_owner"] is first["lease_until"] is None
    assert first["execution_domain_id"] == case.domain_id
    assert first["recovery_required"] is True
    assert first["recovery_version"] == 1
    assert first["recovery"] == {
        **parse_json_object(before["recovery"]),
        "reason": "lease_expired",
    }
    for field in (
        "id",
        "worker_id",
        "started_at",
        "deadline",
        "deadline_binding",
        "lease_epoch",
        "desired_state",
        "input_bindings",
        "execution_request",
        "infrastructure_retries",
    ):
        assert first[field] == before[field]
    assert saved["target_held_since"] is not None
    assert [row["kind"] for row in events].count("attempt.execution_unknown") == 1
    assert [row["kind"] for row in events].count("attempt.recovery_required") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("already_uncertain", [False, True])
async def test_closed_finishing_lease_does_not_reopen_execution(
    database: Database, already_uncertain: bool
) -> None:
    case = await execution_case(database, status="finishing")
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == case.attempt_id)
            .values(
                execution_state="stopped",
                recovery_required=already_uncertain,
                outcome_payload_state="not_applicable",
                outcome={
                    "version": 1,
                    "outcome": "succeeded",
                    "error_class": None,
                    "error_code": None,
                    "lost_reason": None,
                    "reporting_dispatcher_id": case.grant_id,
                    "reporting_lease_epoch": 1,
                    "replay_digest": "a" * 64,
                    "response": {"status": "finishing", "desired_state": "run"},
                    "error_payload": None,
                },
            )
        )
        await writer.execute(
            update(attempt_dispatcher)
            .where(attempt_dispatcher.c.id == case.grant_id)
            .values(closed_at=case.promotion.clock.now())
        )
    case.promotion.clock.advance(30)

    async with database.transaction() as writer:
        changed = await recover_expired_lease(
            await case.context(writer, False), case.attempt_id
        )
    saved = await case.snapshot(database)

    assert changed
    assert saved["execution_state"] == "stopped"
    assert saved["status"] == "finishing"
    assert saved["recovery_required"] is already_uncertain
    assert saved["recovery_version"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("historical_grant", [False, True])
async def test_only_never_dispatched_created_attempt_can_close_without_runtime(
    database: Database, historical_grant: bool
) -> None:
    case = await execution_case(database, timeout="0s", granted=historical_grant)
    async with database.transaction() as writer:
        if historical_grant:
            await writer.execute(
                update(attempt)
                .where(attempt.c.id == case.attempt_id)
                .values(
                    status="created",
                    execution_state="not_started",
                    execution_domain_id=None,
                    worker_id=None,
                    lease_owner=None,
                    lease_until=None,
                    lease_epoch=0,
                    started_at=None,
                )
            )
            await writer.execute(
                update(attempt_dispatcher)
                .where(attempt_dispatcher.c.id == case.grant_id)
                .values(closed_at=case.promotion.clock.now())
            )

    async with database.transaction() as writer:
        context = await case.context(writer)
        await expire_run(context)
        closed = await close_undispatched_attempt(context, case.attempt_id)
    saved = await case.snapshot(database)

    assert closed is not historical_grant
    assert saved["status"] == ("created" if historical_grant else "cancelled")


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_status", ["pending", "approved"])
async def test_run_expiry_closes_wait_observation_and_delivery_atomically(
    database: Database, audit_status: str
) -> None:
    case = await promotion_case(database, timeout="0s")
    await case.promote(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        node_id = await writer.scalar(
            select(node_instance.c.id)
            .where(node_instance.c.run_id == case.run_id)
            .order_by(node_instance.c.id)
            .limit(1)
        )
        wait_id, approval_id = seed.new_id(), seed.new_id()
        await writer.execute(
            insert(wait).values(
                id=wait_id,
                node_instance_id=node_id,
                attempt_id=None,
                kind="approval",
                wake_at=None,
                subscription=None,
                reason=None,
                status="open",
                observation={
                    "current": {"token": "old"},
                    "last_success_at": "original",
                },
                evaluation_error=None,
                **seed.stamps(),
            )
        )
        await writer.execute(
            insert(approval_request).values(
                id=approval_id,
                node_instance_id=node_id,
                wait_id=wait_id,
                channel="fake",
                channel_request_id="remote-1",
                delivery={
                    "desired_state": "open",
                    "revision": 4,
                    "external_ids": ["remote-1"],
                    "authorizations": [{"state": "unknown"}],
                },
                form={},
                bound_digests=None,
                run_cost={},
                status=audit_status,
                response=None,
                approver=None,
                **seed.stamps(),
            )
        )

    async with database.transaction() as writer:
        await observe_run_expiry(await expiry_context(writer, case, False))
    observed, _, _ = await case.snapshot(database)
    async with database.read_transaction() as reader:
        with await reader.execute(select(wait).where(wait.c.id == wait_id)) as result:
            waiting = dict(result.mappings().one())
        with await reader.execute(
            select(approval_request).where(approval_request.c.id == approval_id)
        ) as result:
            approval = dict(result.mappings().one())
    async with database.transaction() as writer:
        await expire_run(await expiry_context(writer, case))
    async with database.read_transaction() as reader:
        with await reader.execute(select(wait).where(wait.c.id == wait_id)) as result:
            settled_wait = dict(result.mappings().one())
        with await reader.execute(
            select(approval_request).where(approval_request.c.id == approval_id)
        ) as result:
            settled_approval = dict(result.mappings().one())

    assert observed["status"] == "running"
    assert waiting["status"] == "cancelled"
    assert waiting["observation"] == {"current": None, "last_success_at": "original"}
    assert approval["status"] == (
        "cancelled" if audit_status == "pending" else "approved"
    )
    assert approval["delivery"] == {
        "desired_state": "closed",
        "revision": 5,
        "external_ids": ["remote-1"],
        "authorizations": [{"state": "unknown"}],
    }
    assert approval["channel_request_id"] == "remote-1"
    assert settled_wait == waiting
    assert settled_approval == approval


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_expiry_rollback_and_cancellation_preserve_original_state(
    database: Database, failure: type[BaseException]
) -> None:
    case = await execution_case(database, timeout="0s")
    before = await case.snapshot(database)
    before_run = await case.promotion.snapshot(database)

    with pytest.raises(failure):
        await abort_expiry(database, case, failure)
    after = await case.snapshot(database)
    after_run = await case.promotion.snapshot(database)

    assert after == before
    assert after_run == before_run
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_expiry_refuses_unadmitted_or_foreign_run(database: Database) -> None:
    case = await execution_case(database, timeout="0s")
    async with database.transaction() as writer:
        await writer.execute(
            update(run_promotion_admission)
            .where(run_promotion_admission.c.run_id == case.promotion.run_id)
            .values(run_bytes=2_000_000)
        )

    async with database.transaction() as writer:
        refused = await expire_run(await case.context(writer))
    async with database.transaction() as writer:
        context = await case.context(writer)
        foreign = replace(
            context,
            scope=replace(context.scope, namespace_id=case.promotion.identifiers.new()),
        )
        with pytest.raises(ExpiryFailed):
            await recover_expired_lease(foreign, case.attempt_id)
    saved, _, _ = await case.promotion.snapshot(database)

    assert refused is ExpiryState.REFUSED
    assert saved["status"] == "running"
    assert saved["deadline_expired_at"] is None


async def abort_expiry(
    database: Database, case: ExecutionCase, failure: type[BaseException]
) -> None:
    async with database.transaction() as writer:
        await expire_run(await case.context(writer))
        raise failure()


@pytest.mark.asyncio
async def test_deadline_crossing_during_guard_stays_expired_after_clock_rollback(
    database: Database,
) -> None:
    case = await execution_case(database, timeout="60s")
    cutoff = case.promotion.clock.now() + timedelta(seconds=60)
    binding = ExecutionDeadline(
        at=cutoff,
        scope_kind="run",
        scope_id=case.promotion.run_id,
        clock_uncertainty=timedelta(0),
        grace=timedelta(seconds=30),
    )
    samples = iter((cutoff - timedelta(microseconds=1), cutoff))
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == case.attempt_id)
            .values(
                deadline_binding=deadline_document(prepare_deadline_record(binding))
            )
        )

    async with database.transaction() as writer:
        context = replace(
            await case.context(writer, False), clock=lambda: next(samples)
        )
        crossed = await observe_attempt_expiry(context, case.attempt_id)
    async with database.transaction() as writer:
        after_rollback = await observe_attempt_expiry(
            await case.context(writer, False), case.attempt_id
        )
    saved, _, _ = await case.promotion.snapshot(database)
    physical = await case.snapshot(database)

    assert crossed is after_rollback is ExpiryState.EXPIRED
    assert saved["deadline_expired_at"] == cutoff
    assert saved["needs_scheduling"] is True
    assert physical["desired_state"] == "cancel"


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["dispatcher", "epoch", "domain", "open"])
async def test_finishing_exception_requires_original_closed_reporting_grant(
    database: Database, mismatch: str
) -> None:
    case = await execution_case(database, status="finishing")
    async with database.transaction() as writer:
        seed = Seeder(writer)
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == case.attempt_id)
            .values(
                execution_state="stopped",
                outcome_payload_state="not_applicable",
                outcome={
                    "version": 1,
                    "outcome": "succeeded",
                    "error_class": None,
                    "error_code": None,
                    "lost_reason": None,
                    "reporting_dispatcher_id": seed.new_id()
                    if mismatch == "dispatcher"
                    else case.grant_id,
                    "reporting_lease_epoch": 0 if mismatch == "epoch" else 1,
                    "replay_digest": "a" * 64,
                    "response": {"status": "finishing", "desired_state": "run"},
                    "error_payload": None,
                },
            )
        )
        await writer.execute(
            update(attempt_dispatcher)
            .where(attempt_dispatcher.c.id == case.grant_id)
            .values(
                closed_at=None if mismatch == "open" else case.promotion.clock.now(),
                execution_domain_id=seed.new_id()
                if mismatch == "domain"
                else case.domain_id,
            )
        )
    case.promotion.clock.advance(30)

    async with database.transaction() as writer:
        recovered = await recover_expired_lease(
            await case.context(writer, False), case.attempt_id
        )
    after = await case.snapshot(database)

    assert recovered
    assert after["execution_state"] == "unknown"
    assert after["recovery_required"] is True
    assert after["execution_domain_id"] == case.domain_id


@pytest.mark.asyncio
async def test_discovered_lease_rechecks_renewal_before_recovery(
    database: Database,
) -> None:
    case = await execution_case(database)
    case.promotion.clock.advance(30)
    async with database.read_transaction() as reader:
        discovered = await DueAttemptSweep("lease").next_page(
            reader, case.promotion.clock.now()
        )
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == case.attempt_id)
            .values(lease_until=case.promotion.clock.now() + timedelta(seconds=120))
        )
    before = await case.snapshot(database)

    async with database.transaction() as writer:
        recovered = await recover_expired_lease(
            await case.context(writer, False), case.attempt_id
        )
    after = await case.snapshot(database)

    assert not recovered
    assert discovered == ((case.attempt_id, case.promotion.run_id),)
    assert after == before
